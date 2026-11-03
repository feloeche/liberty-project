# Empaquetado de la Lambda. Por qué un paso propio y no el builder de SAM:
# las dependencias nativas (pydantic-core, jiter) deben ser wheels Linux ARM64,
# y `uv` las baja para esa plataforma desde macOS sin Docker.
# boto3/botocore NO se empaquetan: ya vienen en el runtime python3.12 de Lambda.
# No se usa `sam build`: su copia omitía el binario de pydantic_core y la Lambda
# fallaba al importar. `sam deploy --template-file template.yaml` empaqueta .build/app tal cual.
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
