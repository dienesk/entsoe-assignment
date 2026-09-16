# Private, encrypted, versioned bucket holding the scraped CSVs (kept
# indefinitely) and the raw JSON responses (expired after N days -- they
# exist to allow reprocessing if the flattening logic changes, not as a
# permanent archive).

resource "aws_s3_bucket" "output" {
  bucket = "${var.name_prefix}-output-${data.aws_caller_identity.current.account_id}"

  tags = var.tags
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_public_access_block" "output" {
  bucket = aws_s3_bucket.output.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "output" {
  bucket = aws_s3_bucket.output.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "output" {
  bucket = aws_s3_bucket.output.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "output" {
  bucket = aws_s3_bucket.output.id

  # Raw JSON responses are kept only so a changed flattener can reprocess
  # them, so they age out; the flattened CSVs are kept indefinitely. The
  # Lambda writes raw payloads under a single top-level "raw/" prefix
  # (lambda/src/csv_writer.py::build_s3_key) precisely so this rule can
  # select them by prefix.
  rule {
    id     = "expire-raw-json"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    expiration {
      days = var.raw_json_expiration_days
    }

    # Versioning is enabled on this bucket, so the expiration above only
    # writes a delete marker and leaves the actual payload behind as a
    # noncurrent version. Without this block the raw data is never really
    # deleted and storage grows without bound.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  # Clean up the delete markers the rule above leaves behind once their last
  # noncurrent version is gone, so listings don't fill up with tombstones.
  rule {
    id     = "clean-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }
}
