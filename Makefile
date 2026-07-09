IMAGE := semaphore-mcp

.PHONY: build test run gen list

gen:
	python3 generate.py

list: gen
	python3 server.py --list

build:
	docker build -t $(IMAGE) .

test: build
	docker run --rm \
	    -v "$(CURDIR)/tests:/app/tests" \
	    --entrypoint python3 \
	    $(IMAGE) -m pytest tests/ -v

run: build
	docker run --rm -i \
	    --env-file "$(CURDIR)/.env" \
	    $(IMAGE)
