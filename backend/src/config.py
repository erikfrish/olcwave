from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    RW_ENABLED: bool 
    RW_API_TOKEN: str = ""
    RW_API_URL: str = ""
    RW_CADDY_TOKEN: str = ""
    # Optional custom CA bundle for the Remnawave API client (PEM file path).
    # Empty = system trust store.
    RW_CA_BUNDLE: str = ""

    # Bounded revocation latency: how long a Remnawave outage may keep
    # previously-verified access alive before containers are removed.
    REMNAWAVE_UNAVAILABLE_GRACE_SECONDS: int = 120
    # How often the background reconciler re-checks users with containers.
    REVOCATION_CHECK_INTERVAL: int = 30
    # Multi-node execution daemons, reachable ONLY via local Unix sockets
    # (no remote TCP Docker); see olcrtc/nodes.py. Shape:
    #   {"vm1": {"docker_url": "unix:///run/olcwave-nodes/vm1.sock",
    #            "tags": ["vm1"], "image": "olcrtc"}, ...}
    # Empty string = single-node local mode (unchanged behavior).
    # Set by deploy env; never committed.
    OLCWAVE_NODES_JSON: str = ""
    # false = the panel host has no Docker daemon (e.g. Kubernetes): no
    # implicit 'local' node and no local Xray routing container. Requires
    # OLCWAVE_NODES_JSON.
    OLCWAVE_LOCAL_NODE: bool = True
    # Optional profile tag -> allowed user short UUIDs; unspecified tags are public.
    OLCWAVE_PROFILE_USERS_JSON: str = "{}"
    DB_HOST: str
    DB_PORT: int
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    
    @property
    def DB_URL(self):
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.POSTGRES_DB}"

    ADMIN_USERNAME: str
    ADMIN_PASSWORD: str

    JWT_SECRET_KEY: str
    JWT_EXPIRE_MINUTES: int = 1440

    RW_SQUAD_NAME: str = ""
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    model_config = SettingsConfigDict(env_file=".env")  # pyright: ignore[reportUnannotatedClassAttribute]

settings = Settings()  # pyright: ignore[reportCallIssue]