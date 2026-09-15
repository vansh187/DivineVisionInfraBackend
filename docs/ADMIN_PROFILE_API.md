# Admin Profile API Integration

Frontend integration guide for the admin panel profile page and header account chip.

All endpoints are admin-only and require:

```http
Authorization: Bearer <admin_access_token>
```

## Get Current Admin Profile

```http
GET /admin/profile
Authorization: Bearer <admin_access_token>
```

Use this endpoint when the admin shell loads and on `/admin/profile` to autopopulate account details.

### Response - 200 OK

```json
{
  "id": "A12345",
  "full_name": "Arjun Mehta",
  "employee_id": "DV1024",
  "email": "arjun@example.com",
  "phone": null,
  "avatar_url": "https://your-project.supabase.co/storage/v1/object/public/admin-profile-photos/A12345/profile_abcd.png",
  "initials": "AM",
  "created_by": "127.0.0.1",
  "created_date": "2026-09-16T02:10:00Z",
  "last_updated_by": "127.0.0.1",
  "last_updated_date": "2026-09-16T02:10:00Z"
}
```

### UI Mapping

| UI field | API field | Notes |
|---|---|---|
| Full name | `full_name` | Use as the main display name |
| Employee ID | `employee_id` | System assigned/admin signup value |
| Email | `email` | Login email; do not edit from the profile form |
| Phone | `phone` | Currently `null`; the admin table does not store phone yet |
| Avatar | `avatar_url` | Public Supabase Storage URL, or `null` until the admin uploads a photo |
| Avatar initials | `initials` | Computed by backend from name/email |

## Upload/Apply Profile Photo

```http
POST /admin/profile/photo
Content-Type: multipart/form-data
Authorization: Bearer <admin_access_token>

file=<jpg-or-png>
```

Use this after the admin chooses a profile image. The backend uploads the file to Supabase Storage, stores the public URL in `divine_admin_users.profile_photo_url`, and returns the updated profile response.

### File Rules

| Rule | Value |
|---|---|
| Field name | `file` |
| Allowed types | `image/jpeg`, `image/png` |
| Max size | `5 MB` |
| Storage bucket | `SUPABASE_ADMIN_PROFILE_PHOTO_BUCKET`, default `admin-profile-photos` |

### Response - 200 OK

Same shape as `GET /admin/profile`, with `avatar_url` populated.

```json
{
  "id": "A12345",
  "full_name": "Arjun Mehta",
  "employee_id": "DV1024",
  "email": "arjun@example.com",
  "phone": null,
  "avatar_url": "https://your-project.supabase.co/storage/v1/object/public/admin-profile-photos/A12345/profile_abcd.png",
  "initials": "AM",
  "created_by": "127.0.0.1",
  "created_date": "2026-09-16T02:10:00Z",
  "last_updated_by": "A12345",
  "last_updated_date": "2026-09-16T02:15:00Z"
}
```

## Frontend Example

```ts
type AdminProfile = {
  id: string;
  full_name: string;
  employee_id: string;
  email: string;
  phone: string | null;
  avatar_url: string | null;
  initials: string;
  created_by: string | null;
  created_date: string | null;
  last_updated_by: string | null;
  last_updated_date: string | null;
};

export async function fetchAdminProfile(token: string) {
  const res = await fetch("/admin/profile", {
    headers: { Authorization: `Bearer ${token}` },
  });

  if (!res.ok) throw new Error(`Failed to load admin profile: ${res.status}`);
  return (await res.json()) as AdminProfile;
}

export async function uploadAdminProfilePhoto(token: string, file: File) {
  const body = new FormData();
  body.append("file", file);

  const res = await fetch("/admin/profile/photo", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body,
  });

  if (!res.ok) throw new Error(`Failed to upload admin profile photo: ${res.status}`);
  return (await res.json()) as AdminProfile;
}
```

## Errors

| HTTP | `detail` | Meaning |
|---|---|---|
| `401` | `missing_token`, `invalid_token`, or `token_expired` | Admin access token is missing/invalid/expired |
| `404` | `not_found` | Token points to an admin account that no longer exists |
| `400` | `empty_file`, `unsupported_file_type`, or `file_too_large` | Profile photo upload failed validation |
| `502` | `storage_not_configured`, `storage_unreachable`, or `storage_upload_failed:<status>` | Supabase Storage upload failed |
| `500` | `internal_error` | Server-side failure |

## Database Notes

Run this database patch once on Supabase/live Postgres before using the upload endpoint:

```bash
python scripts/add_admin_profile_photo_columns.py
```

It applies these idempotent ALTER TABLE changes:

```sql
ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_url varchar(1000);
ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_path varchar(500);
ALTER TABLE divine_admin_users ADD COLUMN IF NOT EXISTS profile_photo_bucket varchar(100);
```

The profile endpoints read from the existing `divine_admin_users` table plus the new photo columns:

- `id`
- `full_name`
- `employee_id`
- `email`
- `profile_photo_url`
- `profile_photo_path`
- `profile_photo_bucket`
- `created_by`
- `created_date`
- `last_updated_by`
- `last_updated_date`

`phone` is still returned as `null`; the admin table does not store phone yet.

## Supabase Storage Notes

Create a public Supabase Storage bucket named `admin-profile-photos`, or set `SUPABASE_ADMIN_PROFILE_PHOTO_BUCKET` to another public bucket name. The backend stores the public object URL in `profile_photo_url` so the admin panel can display it directly.
