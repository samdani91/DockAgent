class MetadataElement:
    def __init__(self, type_, *, key=None, value):
        self.type = type_
        self.key = key
        self.value = value
        self.from_dfile = False
        self.from_ins = False
        self.points = 0

    def equal_value(self, other):
        def norm(v):
            if isinstance(v, list):
                v = str(v).replace("'", "")
            return v.replace(" ", "").replace('"', "")
        return norm(self.value) == norm(other.value)

    def equal_key(self, other):
        return self.key.replace(" ", "") == other.key.replace(" ", "")


class File:
    def __init__(self, path, is_file, is_dir, permissions):
        self.path = path
        self.is_file = is_file
        self.is_dir = is_dir
        self.permissions = permissions
        self.inst_points = 0
        self.path_points = 0
        self.check_command = None

    @property
    def points(self):
        return self.inst_points + self.path_points


class Layer:
    def __init__(self, created_by, comment, files, removed_paths):
        self.created_by = created_by
        self.comment = comment
        self.files = files
        self.removed_paths = removed_paths
        self.workdir = None
        self.inst_info = None

    def set_inst_info(self, inst_info):
        self.inst_info = inst_info
