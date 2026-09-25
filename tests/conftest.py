import logging
import os

import boto3
import pytest

os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
logging.getLogger("strands").setLevel(logging.CRITICAL)

from moto import mock_aws  # noqa: E402

from gwp.store import DynamoStore  # noqa: E402
from gwp.world import seed_world  # noqa: E402


@pytest.fixture
def store():
    with mock_aws():
        s = DynamoStore(boto3.client("dynamodb", region_name="us-east-1"))
        s.create_tables()
        seed_world(s)
        yield s
