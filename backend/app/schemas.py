from typing import Optional

from pydantic import BaseModel, Field


class ProbeRequest(BaseModel):
    url: str = Field(..., min_length=5)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=5)
    format_id: Optional[str] = None
    download_dir: Optional[str] = None
    merge_output_format: Optional[str] = None
    subtitle_lang: Optional[str] = None
    subtitle_auto: bool = False
