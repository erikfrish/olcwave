from pydantic import BaseModel

class ContainerSchema(BaseModel):
    id: str
    # Full 64-hex container id: the stable key for global quota accounting
    # (short `id` and `name` may collide across nodes).
    full_id: str = ""
    # Source node name ('local' or a configured OLCWAVE_NODES_JSON key).
    node: str = "local"
    name: str
    short_uuid: str
    config_tag: str
    status: str
    created: str
    # Creation time as a wall-clock epoch (rotation-generation comparison).
    created_epoch: float
    image: str

class NodeStatusSchema(BaseModel):
    node: str
    ok: bool
    error: str | None = None
    containers: int = 0

class ContainersListSchema(BaseModel):
    """Combined listing across nodes with explicit per-node health.

    A node outage is reported here as ok=false with the error text - the
    list NEVER fabricates an empty-all for a dead node.
    """
    nodes: list[NodeStatusSchema]
    containers: list[ContainerSchema]

class ContainerLogsSchema(BaseModel):
    name: str
    logs: str

class ContainerConfigSchema(BaseModel):
    name: str
    config: str

class ContainerStatsSchema(BaseModel):
    name: str
    # Node identity + full container id: differentiate same-named containers
    # across nodes in global quota aggregation (TrafficManager cursors).
    node: str = ""
    container_id: str = ""
    upload_bytes: int = 0
    download_bytes: int = 0
    total_bytes: int = 0
    upload_rate_bps: int = 0
    download_rate_bps: int = 0
