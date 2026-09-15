"""
Creates or verifies the public Supabase Storage bucket used for admin profile
photos.

Safe to run more than once. Requires:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
Optional:
  SUPABASE_ADMIN_PROFILE_PHOTO_BUCKET (default: admin-profile-photos)

Usage:
    python scripts/setup_admin_profile_photo_bucket.py
"""
from _bootstrap import setup
setup()

import os
import requests


DEFAULT_BUCKET = "admin-profile-photos"
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_MIME_TYPES = ["image/jpeg", "image/png"]


def _headers(service_key: str) -> dict:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def main():
    supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.getenv("SUPABASE_ADMIN_PROFILE_PHOTO_BUCKET", DEFAULT_BUCKET)
    if not supabase_url or not service_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    bucket_url = f"{supabase_url}/storage/v1/bucket/{bucket}"
    get_resp = requests.get(bucket_url, headers=_headers(service_key), timeout=30)
    if get_resp.status_code == 200:
        print(f"Bucket {bucket!r} already exists.")
        return
    if get_resp.status_code not in (400, 404):
        raise RuntimeError(f"bucket_check_failed:{get_resp.status_code}:{get_resp.text[:300]}")

    create_resp = requests.post(
        f"{supabase_url}/storage/v1/bucket",
        headers=_headers(service_key),
        json={
            "id": bucket,
            "name": bucket,
            "public": True,
            "file_size_limit": MAX_BYTES,
            "allowed_mime_types": ALLOWED_MIME_TYPES,
        },
        timeout=30,
    )
    if create_resp.status_code not in (200, 201):
        raise RuntimeError(f"bucket_create_failed:{create_resp.status_code}:{create_resp.text[:300]}")
    print(f"Created public bucket {bucket!r}.")


if __name__ == "__main__":
    main()
