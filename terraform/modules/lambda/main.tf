# The scraper function. Packaged directly from lambda/src with no build step
# (the runtime uses only the standard library plus boto3, which is
# preinstalled in the Lambda Python runtime) and deployed into the private
# subnets so its only route to the internet is through fck-nat.

data "archive_file" "lambda_package" {
  type        = "zip"
  source_dir  = var.lambda_src_dir
  output_path = "${path.module}/build/lambda_package.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}-scraper"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_security_group" "lambda" {
  name        = "${var.name_prefix}-lambda"
  description = "Scraper Lambda: outbound HTTPS only (egress to web-api.tp.entsoe.eu goes via fck-nat)."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to the internet, via fck-nat"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-lambda" })
}

# --- IAM: least-privilege execution role ------------------------------------

# The security token is a SecureString encrypted under the AWS-managed
# aws/ssm key, so reading it needs kms:Decrypt on that key in addition to
# ssm:GetParameter. The parameter itself is created outside Terraform (see
# the root README) so its value never lands in a state file -- which is also
# why it is referenced by name here rather than read through a data source.
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

data "aws_region" "current" {}

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = var.tags
}

# Grants ENI create/describe/delete permissions needed to run the function
# inside a VPC, and the ability to write to any log group/stream (scoped
# further by the log group's own name in the inline policy below is not
# possible with this AWS-managed policy, so we additionally scope our own
# CloudWatch Logs statement to just this function's log group for
# defense-in-depth).
resource "aws_iam_role_policy_attachment" "vpc_access" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "lambda_inline" {
  statement {
    sid       = "WriteOwnLogGroup"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid = "WriteScrapedData"
    # PutObjectTagging covers the x-amz-tagging header sent alongside raw
    # JSON uploads (see lambda/src/csv_writer.py), which S3 treats as a
    # distinct permission from a plain PutObject.
    actions   = ["s3:PutObject", "s3:PutObjectTagging"]
    resources = ["${var.output_bucket_arn}/*"]
  }

  statement {
    sid     = "ReadEndpointConfig"
    actions = ["ssm:GetParameter", "ssm:GetParametersByPath"]
    # Strictly the config path and its children. A bare "<prefix>*" wildcard
    # would also match unrelated siblings such as "<prefix>-secret"; the path
    # itself is included because GetParametersByPath is authorized against
    # the path being queried, not only the parameters it returns.
    resources = [
      "arn:aws:ssm:*:*:parameter${var.ssm_config_prefix}",
      "arn:aws:ssm:*:*:parameter${var.ssm_config_prefix}/*",
    ]
  }

  statement {
    sid       = "ReadSecurityToken"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:*:*:parameter${var.security_token_parameter_name}"]
  }

  statement {
    sid       = "DecryptSecurityToken"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]

    # The aws/ssm key is usable by anything in the account that SSM fronts,
    # so pin this grant to decrypts that actually go through SSM.
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  name   = "${var.name_prefix}-lambda-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_inline.json
}

# --- Function ----------------------------------------------------------------

resource "aws_lambda_function" "scraper" {
  function_name = "${var.name_prefix}-scraper"
  role          = aws_iam_role.lambda.arn

  filename         = data.archive_file.lambda_package.output_path
  source_code_hash = data.archive_file.lambda_package.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  timeout          = var.timeout_seconds
  memory_size      = var.memory_mb

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  # AWS advanced logging controls. JSON format makes the runtime emit each
  # record as a structured object (timestamp, level, message, logger,
  # requestId) and is what lets Lambda filter by level at all -- level
  # filtering does not work with the plain-text format. With this set, the
  # level belongs here and not in a setLevel() call, which is why the
  # function's environment deliberately carries no LOG_LEVEL: code that set
  # it would silently override whatever is deployed.
  logging_config {
    log_format            = "JSON"
    application_log_level = var.application_log_level
    system_log_level      = var.system_log_level
    log_group             = aws_cloudwatch_log_group.lambda.name
  }

  environment {
    variables = {
      OUTPUT_BUCKET            = var.output_bucket_name
      SSM_CONFIG_PREFIX        = var.ssm_config_prefix
      SECURITY_TOKEN_PARAMETER = var.security_token_parameter_name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.vpc_access,
    aws_iam_role_policy.lambda_inline,
    aws_cloudwatch_log_group.lambda,
  ]

  tags = var.tags
}
