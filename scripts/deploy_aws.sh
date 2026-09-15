#!/usr/bin/env bash
# ==============================================================================
# scripts/deploy_aws.sh
# Complete zero-cost ($0.00) AWS deployment script for WFM Sell-Timing Advisor.
#
# Sets up:
# 1. Amazon S3 cache bucket (wfm-advisor-cache-<ACCOUNT_ID>) with seed database.
# 2. AWS SSM Parameter Store parameters (Gemini API Key, Discord Webhook).
# 3. Scoped IAM execution role (CloudWatch logs, SSM read, S3 read/write).
# 4. AWS Lambda function (Python 3.12, 512MB RAM, 180s timeout).
# 5. Public Lambda Function URL for HTTPS query endpoint.
# 6. Amazon EventBridge daily scheduled rule (cron at 02:00 UTC).
# 7. CloudWatch 7-day log retention for zero-cost maintenance.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load .env if present
if [ -f "${ROOT_DIR}/.env" ]; then
    echo "Loading configuration from .env..."
    set -a
    source "${ROOT_DIR}/.env"
    set +a
fi

# Configuration
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
FUNCTION_NAME="${FUNCTION_NAME:-wfm-advisor}"
ROLE_NAME="${ROLE_NAME:-wfm-advisor-lambda-role}"
EVENTBRIDGE_RULE_NAME="wfm-advisor-daily-scan"
CRON_EXPRESSION="cron(0 2 * * ? *)"  # 02:00 UTC daily

echo "================================================================================"
echo "🚀 Deploying WFM Sell-Timing Advisor to AWS (${AWS_REGION})"
echo "================================================================================"

# 1. Check AWS CLI prerequisites
if ! command -v aws &>/dev/null; then
    echo "❌ AWS CLI not found! Please install it with: brew install awscli"
    exit 1
fi

echo "Checking AWS credentials..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "✅ Authenticated to AWS Account ID: ${ACCOUNT_ID} in ${AWS_REGION}"

S3_BUCKET="wfm-advisor-cache-${ACCOUNT_ID}-${AWS_REGION}"

# 2. Ensure S3 Cache Bucket exists
echo "--- 1. Setting up S3 Cache Bucket: ${S3_BUCKET} ---"
if ! aws s3api head-bucket --bucket "${S3_BUCKET}" 2>/dev/null; then
    if [ "${AWS_REGION}" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${AWS_REGION}"
    else
        aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${AWS_REGION}" \
            --create-bucket-configuration LocationConstraint="${AWS_REGION}"
    fi
    echo "Created S3 bucket: ${S3_BUCKET}"
else
    echo "S3 bucket ${S3_BUCKET} already exists."
fi

# Upload seed database to S3 if not present
if ! aws s3api head-object --bucket "${S3_BUCKET}" --key "wfm.db" 2>/dev/null; then
    if [ -f "${ROOT_DIR}/db/wfm.db" ]; then
        echo "Uploading local seed db/wfm.db to s3://${S3_BUCKET}/wfm.db..."
        aws s3 cp "${ROOT_DIR}/db/wfm.db" "s3://${S3_BUCKET}/wfm.db"
    fi
fi

# 3. Configure SSM Parameter Store
echo "--- 2. Setting up SSM Parameter Store (Standard Tier = $0.00) ---"
if [ -n "${GEMINI_API_KEY:-}" ] && [ "${GEMINI_API_KEY}" != "your_gemini_api_key_here" ]; then
    echo "Writing GEMINI_API_KEY to SSM Parameter Store (/wfmadvisor/gemini_api_key)..."
    aws ssm put-parameter \
        --name "/wfmadvisor/gemini_api_key" \
        --type "SecureString" \
        --value "${GEMINI_API_KEY}" \
        --overwrite \
        --region "${AWS_REGION}" >/dev/null
else
    echo "Notice: GEMINI_API_KEY not set in shell. Ensure /wfmadvisor/gemini_api_key exists in SSM."
fi

if [ -n "${DISCORD_WEBHOOK_URL:-}" ] && [ "${DISCORD_WEBHOOK_URL}" != "your_discord_webhook_url_here" ]; then
    echo "Writing DISCORD_WEBHOOK_URL to SSM Parameter Store (/wfmadvisor/discord_webhook_url)..."
    aws ssm put-parameter \
        --name "/wfmadvisor/discord_webhook_url" \
        --type "String" \
        --value "${DISCORD_WEBHOOK_URL}" \
        --overwrite \
        --region "${AWS_REGION}" >/dev/null
fi

if [ -n "${DISCORD_PUBLIC_KEY:-}" ] && [ "${DISCORD_PUBLIC_KEY}" != "your_public_key_here" ]; then
    echo "Writing DISCORD_PUBLIC_KEY to SSM Parameter Store (/wfmadvisor/discord_public_key)..."
    aws ssm put-parameter \
        --name "/wfmadvisor/discord_public_key" \
        --type "String" \
        --value "${DISCORD_PUBLIC_KEY}" \
        --overwrite \
        --region "${AWS_REGION}" >/dev/null
fi

if [ -n "${DISCORD_APPLICATION_ID:-}" ] && [ "${DISCORD_APPLICATION_ID}" != "your_application_id_here" ]; then
    echo "Writing DISCORD_APPLICATION_ID to SSM Parameter Store (/wfmadvisor/discord_application_id)..."
    aws ssm put-parameter \
        --name "/wfmadvisor/discord_application_id" \
        --type "String" \
        --value "${DISCORD_APPLICATION_ID}" \
        --overwrite \
        --region "${AWS_REGION}" >/dev/null
fi

# 4. Create or Update IAM Execution Role
echo "--- 3. Configuring IAM Execution Role (${ROLE_NAME}) ---"
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}'

ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text 2>/dev/null || true)
if [ -z "${ROLE_ARN}" ]; then
    echo "Creating IAM Role: ${ROLE_NAME}..."
    ROLE_ARN=$(aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --query Role.Arn --output text)
    echo "Waiting 10s for IAM role propagation..."
    sleep 10
fi

# Attach scoped inline policy
POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ],
      "Resource": "arn:aws:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter/wfmadvisor/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::${S3_BUCKET}/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"
    }
  ]
}
EOF
)

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "wfm-advisor-permissions" \
    --policy-document "${POLICY_DOC}"

# 5. Build Lambda Deployment Package
if [ ! -f "${ROOT_DIR}/build/function.zip" ]; then
    bash "${SCRIPT_DIR}/package_lambda.sh"
else
    echo "Reusing existing ${ROOT_DIR}/build/function.zip (remove it to re-package)."
fi

# Upload package to S3 for reliable Lambda deployment
echo "Staging deployment zip to s3://${S3_BUCKET}/build/function.zip..."
aws s3 cp "${ROOT_DIR}/build/function.zip" "s3://${S3_BUCKET}/build/function.zip"

# 6. Deploy / Update Lambda Function
echo "--- 5. Deploying Lambda Function (${FUNCTION_NAME}) ---"
ENV_VARS="Variables={DB_PATH=/tmp/wfm.db,S3_CACHE_BUCKET=${S3_BUCKET},S3_CACHE_KEY=wfm.db}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null; then
    echo "Updating existing Lambda function code..."
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --s3-bucket "${S3_BUCKET}" \
        --s3-key "build/function.zip" \
        --region "${AWS_REGION}" >/dev/null

    echo "Waiting for Lambda code update to complete..."
    aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"

    echo "Updating Lambda configuration..."
    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 180 \
        --memory-size 512 \
        --environment "${ENV_VARS}" \
        --region "${AWS_REGION}" >/dev/null
else
    echo "Creating new Lambda function: ${FUNCTION_NAME}..."
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --role "${ROLE_ARN}" \
        --handler "lambda_handler.lambda_handler" \
        --code S3Bucket="${S3_BUCKET}",S3Key="build/function.zip" \
        --timeout 180 \
        --memory-size 512 \
        --environment "${ENV_VARS}" \
        --region "${AWS_REGION}" >/dev/null
fi

# 7. Configure Lambda Function URL (Public HTTPS endpoint)
echo "--- 6. Configuring Public Lambda Function URL ---"
URL_CONFIG=$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null || true)
if [ -z "${URL_CONFIG}" ]; then
    echo "Creating Lambda Function URL (auth: NONE)..."
    FUNCTION_URL=$(aws lambda create-function-url-config \
        --function-name "${FUNCTION_NAME}" \
        --auth-type NONE \
        --cors '{"AllowOrigins":["*"],"AllowMethods":["GET","POST"]}' \
        --query FunctionUrl --output text \
        --region "${AWS_REGION}")

    aws lambda add-permission \
        --function-name "${FUNCTION_NAME}" \
        --statement-id "FunctionURLAllowPublicAccess" \
        --action "lambda:InvokeFunctionUrl" \
        --principal "*" \
        --function-url-auth-type NONE \
        --region "${AWS_REGION}" 2>/dev/null || true

    aws lambda add-permission \
        --function-name "${FUNCTION_NAME}" \
        --statement-id "FunctionURLAllowInvokeFunction" \
        --action "lambda:InvokeFunction" \
        --principal "*" \
        --region "${AWS_REGION}" 2>/dev/null || true
else
    FUNCTION_URL=$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" --query FunctionUrl --output text --region "${AWS_REGION}")
fi

# 8. Configure Amazon EventBridge Rule (Daily Cron at 02:00 UTC)
echo "--- 7. Configuring Amazon EventBridge Scheduled Rule ---"
RULE_ARN=$(aws events put-rule \
    --name "${EVENTBRIDGE_RULE_NAME}" \
    --schedule-expression "${CRON_EXPRESSION}" \
    --description "Daily 02:00 UTC market evaluation scan for WFM Sell-Timing Advisor" \
    --region "${AWS_REGION}" \
    --query RuleArn --output text)

LAMBDA_ARN=$(aws lambda get-function --function-name "${FUNCTION_NAME}" --query Configuration.FunctionArn --output text --region "${AWS_REGION}")

# Add permission for EventBridge to invoke Lambda
aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id "EventBridgeDailyCronInvocation" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "${RULE_ARN}" \
    --region "${AWS_REGION}" 2>/dev/null || true

# Put Target on rule
aws events put-targets \
    --rule "${EVENTBRIDGE_RULE_NAME}" \
    --targets "Id=1,Arn=${LAMBDA_ARN},Input='{\"mode\":\"batch\"}'" \
    --region "${AWS_REGION}" >/dev/null

# 9. Set CloudWatch Log Retention to 7 Days
echo "--- 8. Enforcing 7-Day CloudWatch Log Retention ---"
aws logs put-retention-policy \
    --log-group-name "/aws/lambda/${FUNCTION_NAME}" \
    --retention-in-days 7 \
    --region "${AWS_REGION}" 2>/dev/null || true

echo "================================================================================"
echo "🎉 DEPLOYMENT COMPLETE!"
echo "================================================================================"
echo "🌐 Public Function URL: ${FUNCTION_URL}"
echo "📅 EventBridge Schedule: Daily at 02:00 UTC (${CRON_EXPRESSION})"
echo "🗄️ S3 Cache Bucket: s3://${S3_BUCKET}"
echo ""
echo "Test your live API with:"
echo "curl \"${FUNCTION_URL}?query=rhino+prime\""
echo "================================================================================"
