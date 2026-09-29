from pathlib import Path
from tempfile import TemporaryDirectory


def write_project(destination: Path, contents: dict[str, str]) -> Path:
    """Publish only complete new projects; never replace an existing directory."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'项目目录已存在，拒绝覆盖: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.astra-build-', dir=destination.parent) as temporary:
        staging = Path(temporary) / destination.name
        staging.mkdir()
        for relative, content in contents.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        # rename does not replace populated directories; destination is checked
        # again before publication to also reject a newly created empty folder.
        if destination.exists():
            raise FileExistsError(f'项目目录已存在，拒绝覆盖: {destination}')
        staging.rename(destination)
    return destination / 'project.yaml'
