from pydantic import BaseModel, Field


class ProviderInfo(BaseModel):
    name: str = Field(..., description="Provider display name")
    endpoint: str = Field(..., description="Provider endpoint slug used in API calls")


class ProviderList(BaseModel):
    providers: list[ProviderInfo] = Field(..., description="List of active providers")


class ErrorDetail(BaseModel):
    error: str = Field(..., description="Error code")
    detail: str = Field(..., description="Human-readable error description")
