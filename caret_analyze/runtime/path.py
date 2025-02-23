# Copyright 2021 Research Institute of Systems Planning, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from logging import getLogger
from typing import Dict, List, Optional, Sequence, Union

from .callback import CallbackBase
from .communication import Communication
from .node_path import NodePath
from .path_base import PathBase
from ..common import Columns, Summarizable, Summary, Util
from ..exceptions import InvalidArgumentError, InvalidRecordsError
from ..record.record import merge_sequencial, RecordsInterface
from ..value_objects import PathStructValue

logger = getLogger(__name__)


class ColumnMerger():

    def __init__(self) -> None:
        self._count: Dict[str, int] = {}
        self._column_names: List[str] = []

    def append_column(
        self,
        records: RecordsInterface,
        column_name: str
    ) -> str:
        key = column_name

        if key == records.columns[0] and len(self._count) > 0 and key in self._count.keys():
            count = max(self._count.get(key, 0) - 1, 0)
            return self._to_column_name(count, key)

        if key not in self._count.keys():
            self._count[key] = 0

        column_name = self._to_column_name(self._count[key], key)
        self._column_names.append(column_name)
        self._count[key] += 1
        return column_name

    def append_columns(
        self,
        records: RecordsInterface,
    ) -> List[str]:
        renamed_columns: List[str] = []
        for column in records.columns:
            renamed_columns.append(
                self.append_column(records, column)
            )
        return renamed_columns

    def append_columns_and_return_rename_rule(
        self,
        records: RecordsInterface,
    ) -> Dict[str, str]:
        renamed_columns: List[str] = self.append_columns(records)
        return self._to_rename_rule(records.columns, renamed_columns)

    @property
    def column_names(
        self
    ) -> List[str]:
        return self._column_names

    @staticmethod
    def _to_column_name(
        count: int,
        tracepoint_name: str,
    ) -> str:
        return f'{tracepoint_name}/{count}'

    @staticmethod
    def _to_rename_rule(
        old_columns: Sequence[str],
        new_columns: Sequence[str],
    ):
        return {
            old_column: new_column
            for old_column, new_column
            in zip(old_columns, new_columns)
        }


class RecordsMerged:

    def __init__(
        self,
        merge_targets: List[RecordsInterface],
    ) -> None:
        if len(merge_targets) == 0:
            raise InvalidArgumentError('There are no records to be merged.')
        self._data = self._merge_records(merge_targets)

    @property
    def data(self) -> RecordsInterface:
        return self._data

    @staticmethod
    def _merge_records(
        targets: List[RecordsInterface]
    ) -> RecordsInterface:
        logger.info('Started merging path records.')

        column_merger = ColumnMerger()
        if len(targets[0].data) == 0:
            targets = targets[1:]

        first_element = targets[0]
        left_records = first_element

        rename_rule = column_merger.append_columns_and_return_rename_rule(
            left_records)

        left_records.rename_columns(rename_rule)
        first_column = first_element.columns[0]

        for target in targets[1:]:
            right_records = target

            is_dummy_records = len(right_records.columns) == 0

            if is_dummy_records:
                if target == targets[-1]:
                    msg = 'Detected dummy_records. merge terminated.'
                    logger.info(msg)
                else:
                    msg = 'Detected dummy_records before merging end_records. merge terminated.'
                    logger.warn(msg)
                break
            rename_rule = column_merger.append_columns_and_return_rename_rule(
                right_records)
            right_records.rename_columns(rename_rule)

            if left_records.columns[-1] != right_records.columns[0]:
                raise InvalidRecordsError('left columns[-1] != right columns[0]')
            left_stamp_key = left_records.columns[-1]
            right_stamp_key = right_records.columns[0]

            right_records.drop_columns([left_records.columns[0]])
            right_stamp_key = right_records.columns[0]

            logger.info(
                '\n[merge_sequencial] \n'
                f'- left_column: {left_stamp_key} \n'
                f'- right_column: {right_stamp_key} \n'
            )

            left_records = merge_sequencial(
                left_records=left_records,
                right_records=right_records,
                join_left_key=None,
                join_right_key=None,
                left_stamp_key=left_stamp_key,
                right_stamp_key=right_stamp_key,
                columns=Columns(left_records.columns +
                                right_records.columns).as_list(),
                how='left_use_latest',
                progress_label='binding: node records'
            )

        logger.info('Finished merging path records.')
        left_records.sort(first_column)

        return left_records


class Path(PathBase, Summarizable):
    def __init__(
        self,
        path: PathStructValue,
        child: List[Union[NodePath, Communication]],
        callbacks: Optional[List[CallbackBase]]
    ) -> None:
        super().__init__()

        self._value = path
        self._validate(child)
        self._child = child
        self._columns_cache: Optional[List[str]] = None
        self._callbacks = callbacks
        return None

    def _to_records_core(self) -> RecordsInterface:
        self._verify_path(self.node_paths)
        records = [e.to_records() for e in self._child]
        return RecordsMerged(records).data

    @staticmethod
    def _verify_path(
        path_children: List[NodePath]
    ) -> None:
        for child in path_children[1: -1]:
            if len(child.column_names) == 0:
                msg = 'Node latency is empty. To see more details, execute [ path.verify() ].'
                logger.warning(msg)

    def verify(self) -> bool:
        is_valid = True
        for child in self.node_paths[1:-1]:
            if child.message_context is not None:
                continue
            msg = 'Detected invalid message context. Correct these node_path definitions. \n'
            msg += 'To see node definition,'
            msg += f"execute [ app.get_node('{child.node_name}').summary.pprint() ] \n"
            msg += str(child.summary)
            logger.warning(msg)
            is_valid = False
        return is_valid

    def get_child(self, name: str):
        if not isinstance(name, str):
            raise InvalidArgumentError('Argument type is invalid.')

        def is_target(child: Union[NodePath, Communication]):
            if isinstance(child, NodePath):
                return child.node_name == name
            elif isinstance(child, Communication):
                return child.topic_name == name

        return Util.find_one(is_target, self.child)

    @property
    def summary(self) -> Summary:
        return self._value.summary

    @property
    def callbacks(self) -> Optional[List[CallbackBase]]:
        return self._callbacks

    @staticmethod
    def _validate(path_elements: List[Union[NodePath, Communication]]):
        if len(path_elements) == 0:
            return
        t = NodePath if isinstance(
            path_elements[0], NodePath) else Communication
        for e in path_elements[1:]:
            if t == Communication:
                t = NodePath
            else:
                t = Communication
            if isinstance(e, t):
                continue
            msg = 'NodePath and Communication should be alternated.'
            raise InvalidArgumentError(msg)

    @property
    def path_name(self) -> Optional[str]:
        return self._value.path_name

    def clear_cache(self) -> None:
        self._columns_cache = None
        return super().clear_cache()

    def __str__(self) -> str:
        node_names = [n.node_name for n in self.node_paths]
        return '\n'.join(node_names)

    @property
    def communications(self) -> List[Communication]:
        return Util.filter_items(lambda x: isinstance(x, Communication), self._child)

    @property
    def node_paths(self) -> List[NodePath]:
        return Util.filter_items(lambda x: isinstance(x, NodePath), self._child)

    @property
    def topic_names(self) -> List[str]:
        return sorted(self._value.topic_names)

    @property
    def child(self) -> List[Union[NodePath, Communication]]:
        return self._child

    @property
    def child_names(self) -> List[str]:
        return sorted(self._value.child_names)

    @property
    def node_names(self) -> List[str]:
        return sorted(self._value.node_names)
