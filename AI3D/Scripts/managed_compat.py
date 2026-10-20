"""Small repeatable compatibility patches; preserve upstream logic and backups."""
import ast
import shutil


def install_managed_compat(root,wrapper):
    destination=root/'ComfyUI/custom_nodes/Game3DManaged'
    destination.mkdir(exist_ok=True)
    shutil.copyfile(root/'Scripts/managed_nodes.py',destination/'__init__.py')
    backup=root/'Backups/phase_1_2/vendor';backup.mkdir(parents=True,exist_ok=True)
    convert=wrapper/'hy3dpaint/convert_utils.py'
    post=next(p for p in wrapper.rglob('postprocessors.py') if 'class DegenerateFaceRemover' in p.read_text('utf-8'))
    for p in (convert,post):
        saved=backup/p.name
        if not saved.exists():shutil.copyfile(p,saved)
    text=post.read_text('utf-8')
    if '# G3DC: actual degenerate filtering' not in text:
        tree=ast.parse(text);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DegenerateFaceRemover')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__call__')
        replacement='''    def __call__(self, mesh):
        # G3DC: actual degenerate filtering; preserve UV/material through Trimesh.
        import trimesh
        if not isinstance(mesh, trimesh.Trimesh):
            raise TypeError('Degenerate filtering requires a Trimesh')
        mesh = mesh.copy()
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.remove_unreferenced_vertices()
        return mesh
'''
        lines=text.splitlines(keepends=True);lines[method.lineno-1:method.end_lineno]=[replacement];post.write_text(''.join(lines),'utf-8')
    text=convert.read_text('utf-8')
    if '# G3DC: isolated PBR intermediates' not in text:
        tree=ast.parse(text);fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='create_glb_with_pbr_materials')
        start=fn.body[1].lineno-1 if isinstance(fn.body[0],ast.Expr) and isinstance(fn.body[0].value,ast.Constant) else fn.body[0].lineno-1
        lines=text.splitlines(keepends=True)
        body=''.join(lines[start:fn.end_lineno]).replace('temp_glb = "temp.glb"','temp_glb = os.path.join(temp_dir, "temp.glb")').replace('mr_combined_path = "mr_combined.png"','mr_combined_path = os.path.join(temp_dir, "mr_combined.png")')
        lines[start:fn.end_lineno]=['    # G3DC: isolated PBR intermediates\n    with tempfile.TemporaryDirectory(prefix="g3dc_pbr_", dir=os.path.dirname(os.path.abspath(output_path))) as temp_dir:\n'+''.join('    '+line if line.strip() else line for line in body.splitlines(keepends=True))]
        convert.write_text('import os\nimport tempfile\n'+''.join(lines),'utf-8')
    print('Managed stage nodes and isolated PBR temporaries configured')
