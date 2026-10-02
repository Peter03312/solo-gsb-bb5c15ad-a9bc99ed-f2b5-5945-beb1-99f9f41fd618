"""HTTP 请求/响应模型与输入校验。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from . import geometry


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    if not math.isfinite(float(value)):
        raise ValueError(f"{label}必须是有限数，不能为 NaN/Infinity")
    return float(value)


def _require_utc(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{field}必须带时区信息（请使用 UTC，例如 2026-09-30T00:00:00Z）")
    return dt.astimezone(timezone.utc)


class PointIn(BaseModel):
    x: float
    y: float
    t: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("x", "y", mode="before")
    @classmethod
    def _check_coord(cls, v):
        return _finite(v, "坐标")

    @field_validator("t")
    @classmethod
    def _check_t(cls, v: datetime) -> datetime:
        return _require_utc(v, "轨迹点时刻")


class RegionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    polygon: list[list[float]] = Field(description="简单多边形顶点 [[x,y], ...]，可闭合可不闭合")
    valid_from: datetime
    valid_to: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        # 先做有限数检查，再交给 Shapely 做简单多边形判定
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)

    def validate_time_order(self) -> None:
        if self.valid_from >= self.valid_to:
            raise ValueError(
                "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )


class RegionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    polygon: list[list[float]] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v, info):
        return None if v is None else _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)


class RegionCreateInBatch(BaseModel):
    """批量变更中的登记操作（action 固定为 create）。"""

    action: Literal["create"] = "create"
    name: str = Field(min_length=1, max_length=200)
    polygon: list[list[float]] = Field(description="简单多边形顶点 [[x,y], ...]，可闭合可不闭合")
    valid_from: datetime
    valid_to: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)

    def validate_time_order(self) -> None:
        if self.valid_from >= self.valid_to:
            raise ValueError(
                "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )


class RegionUpdateInBatch(BaseModel):
    """批量变更中的修订操作（action 固定为 update，字段任意组合的部分更新）。"""

    action: Literal["update"] = "update"
    region_id: int = Field(ge=1, alias="id")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    polygon: list[list[float]] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v, info):
        return None if v is None else _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)


class RegionDeleteInBatch(BaseModel):
    """批量变更中的删除操作（action 固定为 delete）。"""

    action: Literal["delete"] = "delete"
    region_id: int = Field(ge=1, alias="id")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


BatchOperation = Annotated[
    RegionCreateInBatch | RegionUpdateInBatch | RegionDeleteInBatch,
    Field(discriminator="action"),
]


class RegionBatchRequest(BaseModel):
    """区域集合的一次性原子变更：登记/修订/删除 + 指定轨迹安全核验。"""

    expected_revision: int = Field(ge=0, description="客户端预期的全局 revision")
    operations: list[BatchOperation] = Field(
        min_length=1, description="区域操作列表，元素为 create/update/delete 之一"
    )
    trajectories: list[list[PointIn]] = Field(
        min_length=1, description="至少一条带时区绝对时刻轨迹；每条至少 2 个点"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("trajectories")
    @classmethod
    def _check_trajectories(cls, v):
        for i, pts in enumerate(v):
            if not isinstance(pts, list) or len(pts) < 2:
                raise ValueError(f"第 {i} 条轨迹至少需要 2 个坐标点")
        return v


class VerifyRequest(BaseModel):
    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class PassIssueRequest(BaseModel):
    """签发通行凭证：一条带时区绝对时刻的连续轨迹（结构同 /api/verify）。"""

    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class ObservationRegisterRequest(BaseModel):
    """登记安全状态观察：一条带时区绝对时刻的连续轨迹（结构同 /api/verify）。

    安全或不安全均可登记；轨迹至少 2 点、时刻严格递增，且首点时刻必须
    晚于登记时刻（服务端拿到写锁后的当前时间）。
    """

    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class RelativePointIn(BaseModel):
    """调度请求中的轨迹点：相对首点的秒数（首点必须为 0）。"""

    x: float
    y: float
    t: float = Field(description="相对首点的秒数；首点必须为 0，其余严格递增")

    model_config = ConfigDict(extra="forbid")

    @field_validator("x", "y", "t", mode="before")
    @classmethod
    def _check_finite(cls, v, info):
        return _finite(v, {"x": "坐标 x", "y": "坐标 y", "t": "相对时刻"}.get(
            info.field_name, info.field_name))


class ScheduleRequest(BaseModel):
    """固定行程的安全出发时刻计算请求。"""

    points: list[RelativePointIn] = Field(min_length=2)
    window_start: datetime = Field(description="候选出发时段起点（含，带时区）")
    window_end: datetime = Field(description="候选出发时段终点（不含，带时区）")

    model_config = ConfigDict(extra="forbid")

    @field_validator("window_start", "window_end")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)


class CommitmentIssueRequest(BaseModel):
    """择时通行承诺签发请求：相对秒数固定行程 + 带时区候选出发窗口。

    轨迹与窗口语义与 /api/schedule 相同（首点相对秒数为 0、其余严格递增；
    窗口左闭右开），且窗口起点必须晚于签发时刻。
    """

    points: list[RelativePointIn] = Field(min_length=2)
    window_start: datetime = Field(description="候选出发窗口起点（含，带时区），须晚于签发时刻")
    window_end: datetime = Field(description="候选出发窗口终点（不含，带时区）")

    model_config = ConfigDict(extra="forbid")

    @field_validator("window_start", "window_end")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)


# ---------------------------------------------------------------- 响应模型

def _iso_z(dt: datetime) -> str:
    """输出 UTC ISO8601 并带 Z 后缀。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _UTCDatetimeModel(BaseModel):
    """所有 datetime 字段统一输出 UTC ISO8601 且带 Z 后缀。"""

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_dt(self, value, _info):  # pragma: no cover - 见具体字段序列化
        if isinstance(value, datetime):
            return _iso_z(value)
        return value


class CrossingOut(_UTCDatetimeModel):
    entry_time: datetime
    exit_time: datetime
    entry_inclusive: bool
    exit_inclusive: bool
    point_contact: bool


class RegionResultOut(_UTCDatetimeModel):
    region_id: int
    region_name: str
    crossings: list[CrossingOut]


class RegionOut(_UTCDatetimeModel):
    id: int
    name: str
    polygon: list[list[float]]
    valid_from: datetime
    valid_to: datetime
    version: int
    created_at: datetime
    updated_at: datetime


class VerifyResponse(_UTCDatetimeModel):
    safe: bool
    revision: int
    checked_at: datetime
    results: list[RegionResultOut]


class DepartureIntervalOut(_UTCDatetimeModel):
    start: datetime
    end: datetime
    start_inclusive: bool
    end_inclusive: bool


class ScheduleResponse(_UTCDatetimeModel):
    revision: int
    window_start: datetime
    window_end: datetime
    intervals: list[DepartureIntervalOut]


class CommitmentOut(_UTCDatetimeModel):
    """择时通行承诺：签发/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消）；
    revision 为签发时核验所用的区域集合版本号。
    intervals 为「当前时刻」视角的安全出发区间（相对 now 裁剪后的
    左闭右开区间）；取消承诺时 intervals 恒为空（承诺已失效）。
    """

    id: int
    status: str
    revision: int
    trajectory: list[RelativePointIn]
    window_start: datetime
    window_end: datetime
    intervals: list[DepartureIntervalOut]
    issued_at: datetime
    cancelled_at: datetime | None


class RegionCreatedResponse(_UTCDatetimeModel):
    region: RegionOut
    revision: int


class BatchOperationResultOut(BaseModel):
    """单个操作的结果。

    create：带新分配的 region_id 与 version=1；
    update：带修订后的 version（原 version +1）；
    delete：只回显被删除的 region_id 与删除前 version。
    """

    index: int
    action: str
    region_id: int
    version: int | None = None


class TrajectoryCheckOut(_UTCDatetimeModel):
    """批量变更中单条轨迹对变更后集合的核验结果。"""

    index: int
    safe: bool
    results: list[RegionResultOut]


class RegionBatchResponse(_UTCDatetimeModel):
    revision: int
    operations: list[BatchOperationResultOut]
    trajectories: list[TrajectoryCheckOut]


class RevisionOut(BaseModel):
    revision: int


class PassPointOut(_UTCDatetimeModel):
    """凭证轨迹点：时刻输出 UTC ISO8601 并带 Z 后缀。"""

    x: float
    y: float
    t: datetime


class PassOut(_UTCDatetimeModel):
    """通行凭证：签发/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消）；
    revision 为签发时核验所用的区域集合版本号。
    """

    id: int
    status: str
    revision: int
    trajectory: list[PassPointOut]
    issued_at: datetime
    cancelled_at: datetime | None


class ObservationOut(_UTCDatetimeModel):
    """安全状态观察：登记/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消）；
    conclusion 为最近一次安全结论（safe/unsafe），conclusion_revision 为
    该结论对应的区域集合版本号（登记时即为当时 revision）；
    registered_at 为登记时刻。
    """

    id: int
    status: str
    trajectory: list[PassPointOut]
    conclusion: str
    revision: int
    registered_at: datetime
    cancelled_at: datetime | None


class ObservationEventOut(_UTCDatetimeModel):
    """观察结论翻转事件：一次成功区域变更导致 safe↔unsafe 翻转的记录。

    seq 为观察内从 1 开始严格递增的事件序号；revision 为引起翻转的区域
    变更之后的新全局 revision。
    """

    seq: int
    old_conclusion: str
    new_conclusion: str
    revision: int
    occurred_at: datetime


class ObservationEventPage(_UTCDatetimeModel):
    """观察事件分页结果：按 seq 升序；next_cursor 为下一页游标（末页为 null）。"""

    observation_id: int
    events: list[ObservationEventOut]
    next_cursor: int | None


class ErrorOut(BaseModel):
    error: dict
