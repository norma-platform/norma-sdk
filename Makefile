.PHONY: check python-check typescript-check build

check: python-check typescript-check

python-check:
	$(MAKE) -C python check

typescript-check:
	$(MAKE) -C typescript check

build:
	$(MAKE) -C python build
	$(MAKE) -C typescript build
