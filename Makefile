.PHONY: test lint smoke clean

test:
	python -m pytest tests/ -v

lint:
	python -m py_compile $$(find src tests -name "*.py")
	python -c "import yaml, glob; [list(yaml.safe_load_all(open(f))) for f in glob.glob('**/*.y*ml', recursive=True) if '.git' not in f]"

smoke:
	PYTHONPATH=src python -m self_healer scan --snapshot examples/snapshot.json
	PYTHONPATH=src python -m self_healer run --policies examples/policies.yaml --snapshot examples/snapshot.json
	PYTHONPATH=src python -m self_healer bundle --kind Deployment --namespace default --name api --snapshot examples/snapshot.json -o /tmp/bundle.json

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache
