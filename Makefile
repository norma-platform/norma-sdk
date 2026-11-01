.PHONY: check schema-check python-check typescript-check build

check: schema-check python-check typescript-check

schema-check:
	python3 tools/sync_instrumentation_schema.py --check

python-check:
	$(MAKE) -C python check

typescript-check:
	$(MAKE) -C typescript check

build:
	$(MAKE) -C python build
	$(MAKE) -C typescript build
