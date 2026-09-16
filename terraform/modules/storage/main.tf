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

  # Raw JSON responses (kept only for reprocessing) live at varying prefixes
  # per endpoint, so a fixed key prefix can't select them -- the Lambda tags
  # each raw upload with data-class=raw (see lambda/src/csv_writer.py) and
  # this rule matches on that tag instead. Flattened CSVs are never tagged
  # this way and so are kept indefinitely.
  rule {
    id     = "expire-raw-json"
    status = "Enabled"

    filter {
      tag {
        key   = "data-class"
        value = "raw"
      }
    }

    expiration {
      days = var.raw_json_expiration_days
    }
  }
}
