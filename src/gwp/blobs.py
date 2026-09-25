"""Where uploaded document bytes live: S3 on AWS, a dict in tests and the offline eval."""

from __future__ import annotations

from typing import Any, Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...


class MemoryBlobs:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        return self.objects[key]


class S3Blobs:
    def __init__(self, bucket: str, client: Any = None, region: str = "us-east-1"):
        import boto3

        self.bucket = bucket
        self.client = client or boto3.client("s3", region_name=region)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        # If the key exists the bytes are identical, since keys are content hashes.
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
