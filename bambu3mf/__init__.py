"""bambu3mf - write Bambu Studio compatible 3MF projects and presets.

Typical use::

    from bambu3mf import ProfileLibrary, build_project, write_3mf
    lib = ProfileLibrary.open()
    project, builder = build_project(spec_dict, lib)
    write_3mf(project, "out.3mf")
"""

__version__ = "0.1.0"

from .profiles import ProfileLibrary, ProfileError, fetch_profiles  # noqa: E402,F401
from .settings import SettingsBuilder, SettingsError  # noqa: E402,F401
from .mesh import Mesh  # noqa: E402,F401
from .project import Project, ModelObject, Part, Instance, LayerRange, Plate  # noqa: E402,F401
from .writer import write_3mf  # noqa: E402,F401
from .reader import read_project, read_meshes  # noqa: E402,F401
from .spec import build_project, SpecError  # noqa: E402,F401
from .validate import validate  # noqa: E402,F401
