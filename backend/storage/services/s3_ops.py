"""S3 object operations with transient RGWSquared credentials.

RGWSquared owns bucket lifecycle and returns the tenant area-mgmt keys through
structureInfo. Django uses those keys only in memory for object operations.
"""

import logging

import boto3
import urllib3
from django.conf import settings

from storage.services.rgw_squared import RGWSquaredClient

logger = logging.getLogger(__name__)

# Development RGW may use a private/self-signed CA; production should verify TLS.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _make_s3_client(access_key, secret_key):
    """Create a boto3 S3 client with the given credentials."""
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=settings.S3_REGION,
        verify=settings.S3_VERIFY_SSL,
    )


def _get_client():
    return RGWSquaredClient(
        base_url=settings.RGWSQUARED_URL,
        username=settings.RGWSQUARED_USERNAME,
        password=settings.RGWSQUARED_PASSWORD,
    )


def _structure_name(tenant):
    return tenant.rgwsquared_structure or tenant.code


def get_structure_info(tenant, client=None):
    """Return RGWSquared structureInfo for a tenant."""
    client = client or _get_client()
    return client.get_structure_info(_structure_name(tenant))


def ensure_structure_initialized(tenant, client=None):
    """Return structureInfo or raise if RGWSquared has no Ceph backing yet."""
    info = get_structure_info(tenant, client=client)
    if not info.get("initialized"):
        raise RuntimeError(f"RGWSquared structure {tenant.code} is not initialized")
    return info


def fetch_mgmt_keys(tenant, client=None):
    """Fetch transient {TENANT}$area-mgmt S3 keys from RGWSquared."""
    info = ensure_structure_initialized(tenant, client=client)
    rgw_user = info.get("rgwintUser") or {}
    access_key = rgw_user.get("access_key")
    secret_key = rgw_user.get("secret_key")
    if not access_key or not secret_key:
        raise RuntimeError(
            f"RGWSquared structureInfo returned no S3 keys for {tenant.code}"
        )
    return access_key, secret_key


def get_mgmt_s3_client(tenant, client=None):
    """S3 client using transient tenant area-mgmt credentials."""
    access_key, secret_key = fetch_mgmt_keys(tenant, client=client)
    return _make_s3_client(access_key, secret_key)


def list_objects(s3_client, bare_name, *, raise_errors=False):
    """List all objects in a bucket. Returns list of {key, size, last_modified}."""
    files = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bare_name):
            for obj in page.get("Contents", []):
                files.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"],
                        "etag": str(obj.get("ETag", "")).strip('"'),
                    }
                )
    except Exception as e:
        if raise_errors:
            raise
        logger.warning(f"Could not list objects in {bare_name}: {e}")
    return files


def upload_object(
    s3_client, bare_name, key, body, content_type="application/octet-stream"
):
    """Upload a file to a bucket."""
    return s3_client.put_object(
        Bucket=bare_name,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


def delete_object(s3_client, bare_name, key):
    """Delete a single object from a bucket."""
    s3_client.delete_object(Bucket=bare_name, Key=key)


def download_object(s3_client, bare_name, key):
    """Download an object. Returns (body_bytes, content_type)."""
    response = s3_client.get_object(Bucket=bare_name, Key=key)
    body = response["Body"].read()
    content_type = response.get("ContentType", "application/octet-stream")
    return body, content_type


def get_all_bucket_stats():
    """Compatibility shim. RGW Admin API credentials are no longer configured."""
    return {}


def get_bucket_stats_for_tenant(tenant, bucket_names):
    """Calculate simple bucket stats using S3 object listing."""
    if not bucket_names:
        return {}
    try:
        s3 = get_mgmt_s3_client(tenant)
    except Exception as e:
        logger.warning(f"Could not create S3 client for stats in {tenant.code}: {e}")
        return {}

    stats = {}
    for name in bucket_names:
        files = list_objects(s3, name)
        stats[name] = {
            "size_bytes": sum(f["size"] for f in files),
            "num_objects": len(files),
        }
    return stats


def parse_rgwsquared_bucket_name(ms_name, tenant_code):
    """Convert RGWSquared bucket reference to bare S3 name.

    RGWSquared returns "NFFADI:275" or "NFFADI/275" in various contexts.
    The bare name for S3 ops (with tenanted credentials) is just "275".
    """
    for prefix in [f"{tenant_code}:", f"{tenant_code}/"]:
        if ms_name.startswith(prefix):
            return ms_name[len(prefix) :]
    return ms_name
