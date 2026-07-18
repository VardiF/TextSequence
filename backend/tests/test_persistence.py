from app.domain.operations import new_project
from app.persistence.project_store import ProjectStore


def test_store_round_trip_and_atomic_file(tmp_path):
    store = ProjectStore(tmp_path)
    project = new_project("Saved")
    store.save(project)
    loaded = store.load(project.id)
    assert loaded.id == project.id
    assert list(tmp_path.glob("*.tmp")) == []
