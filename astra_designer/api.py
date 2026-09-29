from pathlib import Path

from astra_designer.contracts.blueprint import load_blueprint
from astra_designer.contracts.validation import BlueprintError, ValidationResult
from astra_designer.generation.project import generate
from astra_designer.validation.acceptance import verify
from astra_designer.validation.static import validate
from astra_designer.catalog.registry import with_catalog


@with_catalog
def validate_blueprint(path: str | Path) -> ValidationResult:
    path = Path(path).resolve()
    try:
        return validate(load_blueprint(path), path.parent)
    except BlueprintError as exc:
        return exc.result


@with_catalog
def generate_project(path: str | Path, destination: str | Path, *, framework=None, model_config=None) -> Path:
    path = Path(path).resolve()
    blueprint = load_blueprint(path)
    if framework is not None:
        blueprint.setdefault('execution', {})['default_framework'] = framework
    validate(blueprint, path.parent).require_valid()
    import json
    from astra_designer.catalog.registry import catalog_lock
    lock_file = path.parent / 'capabilities.lock.json'
    if lock_file.exists() and json.loads(lock_file.read_text(encoding='utf-8')) != catalog_lock(blueprint):
        raise ValueError('能力描述已变化，请重新设计或更新能力版本，不能静默使用变更实现')
    from astra_designer.frameworks.registry import resolve
    framework_file = path.parent / 'frameworks.lock.json'
    if framework_file.exists():
        previous = json.loads(framework_file.read_text(encoding='utf-8'))
        current = resolve(blueprint)
        # Selection reasons may change when an automatic decision becomes pinned.
        for lock in (previous, current):
            for item in lock['agents'].values():
                item.pop('reason', None)
        if previous != current:
            raise ValueError('框架契约或选择已变化，请重新规划新版本')
    return generate(blueprint, path.parent, Path(destination).absolute(), model_config=model_config)


def verify_run(project_file: str | Path, state: dict) -> dict:
    return verify(Path(project_file).resolve(), state)


@with_catalog
def design_project(goal: str | None, session_dir: str | Path, *, inputs=None, answers=None,
                   client=None, max_attempts: int = 2, requirements=None, execution=None, resume=None) -> dict:
    from astra_designer.pipeline import design
    if client is None:
        from astra_designer.llm.config import configured_model
        client = configured_model()
    return design(goal, session_dir, client, inputs=inputs, answers=answers, max_attempts=max_attempts,
                  requirements=requirements, execution=execution, resume=resume)
