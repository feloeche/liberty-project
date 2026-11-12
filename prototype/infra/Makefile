# Lambda packaging. Why a custom step instead of the SAM builder:
# native dependencies (pydantic-core, jiter) must be Linux ARM64 wheels,
# and `uv` downloads them for that platform from macOS without Docker.
# boto3/botocore are NOT packaged: they already ship with Lambda's python3.12 runtime.
# `sam build` is not used: its copy skipped the pydantic_core binary and the Lambda
# failed on import. `sam deploy --template-file template.yaml` packages .build/app as is.
APP := .build/app

.PHONY: build validate clean

build: clean
	mkdir -p $(APP)
	cp -R ../agil ../data ../prompts ../web $(APP)/
	find $(APP) -name __pycache__ -prune -exec rm -rf {} +
	uv pip install --quiet --target $(APP) --python-version 3.12 \
		--python-platform aarch64-manylinux2014 --only-binary :all: "anthropic>=2.0"

validate:
	uvx --from aws-sam-cli sam validate --lint

clean:
	rm -rf .build .aws-sam
