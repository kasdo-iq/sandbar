
from pydantic import BaseModel


class PresignedUrlPart(BaseModel):
    part_number: int
    url: str

class MultipartUploadRequest(BaseModel):
    file_path: str
    presigned_urls: list[PresignedUrlPart]
    part_size: int

class PartUploadResult(BaseModel):
    part_number: int
    etag: str | None = ""
    success: bool
    error: str | None = None

class MultipartUploadResponse(BaseModel):
    status: str
    message: str
    file_name: str
    parts_results: list[PartUploadResult]
    successful_parts: int
    failed_parts: int
