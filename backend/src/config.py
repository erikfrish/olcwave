from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    RW_ENABLED: bool 
    RW_API_TOKEN: str = ""
    RW_API_URL: str = ""
    # Optional custom CA bundle for the Remnawave API client (PEM file path).
    # Empty = system trust store.
    RW_CA_BUNDLE: str = ""

    # Bounded revocation latency: how long a Remnawave outage may keep
    # previously-verified access alive before containers are removed.
    REMNAWAVE_UNAVAILABLE_GRACE_SECONDS: int = 120
    # How often the background reconciler re-checks users with containers.
    REVOCATION_CHECK_INTERVAL: int = 30
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