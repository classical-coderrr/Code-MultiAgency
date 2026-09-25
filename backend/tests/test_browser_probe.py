"""Exercise the real browser gate against a deliberately slow edit form."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[1]
PLAYWRIGHT = BACKEND.parent / "frontend" / "node_modules" / "playwright"
PROBE = BACKEND / "app" / "workflow" / "browser_probe.cjs"

PAGE = r"""<!doctype html><html><body>
<h1>Slow edit form integration test</h1>
<button data-testid="crud-add">Add</button>
<input data-testid="crud-field-title">
<button data-testid="crud-save">Save</button>
<div id="list"></div>
<script>
let records = [], editing = false;
const title = document.querySelector('[data-testid="crud-field-title"]');
const save = document.querySelector('[data-testid="crud-save"]');
const list = document.getElementById('list');
function render() {
  list.replaceChildren();
  for (const value of records) {
    const row = document.createElement('div');
    row.dataset.testid = 'crud-row';
    row.append(document.createTextNode(value));
    const edit = document.createElement('button');
    edit.dataset.testid = 'crud-edit';
    edit.textContent = 'Edit';
    edit.onclick = () => {
      editing = true;
      title.value = '';
      save.disabled = true;
      setTimeout(() => { title.value = value; save.disabled = false; }, 300);
    };
    const remove = document.createElement('button');
    remove.dataset.testid = 'crud-delete';
    remove.textContent = 'Delete';
    remove.onclick = () => { records = []; render(); };
    row.append(edit, remove);
    list.append(row);
  }
}
document.querySelector('[data-testid="crud-add"]').onclick = () => {
  editing = false;
  title.value = '';
};
save.onclick = () => {
  records = [title.value];
  editing = false;
  title.value = '';
  render();
};
</script></body></html>"""


MULTI_PAGE = r"""<!doctype html><html><body>
<h1>Student and Course management browser test</h1>
<section data-testid="crud-panel-Student"><h2>Students</h2>
<button data-testid="crud-add">Add student</button><input data-testid="crud-field-name">
<button data-testid="crud-save">Save student</button><div class="list"></div></section>
<section data-testid="crud-panel-Course"><h2>Courses</h2>
<button data-testid="crud-add">Add course</button><input data-testid="crud-field-title">
<button data-testid="crud-save">Save course</button><div class="list"></div></section>
<script>
function setup(panelId, fieldName) {
  const panel = [...document.querySelectorAll(`[data-testid="crud-panel-${panelId}"]`)]
    .find(element => element.querySelector('[data-testid="crud-add"]'));
  const field = panel.querySelector(`[data-testid="crud-field-${fieldName}"]`);
  const list = panel.querySelector('.list');
  let records = [];
  function render() {
    list.replaceChildren();
    for (const value of records) {
      const row = document.createElement('div');
      row.dataset.testid = 'crud-row';
      row.append(document.createTextNode(value));
      const edit = document.createElement('button');
      edit.dataset.testid = 'crud-edit';
      edit.textContent = 'Edit';
      edit.onclick = () => setTimeout(() => { field.value = value; }, 100);
      const remove = document.createElement('button');
      remove.dataset.testid = 'crud-delete';
      remove.textContent = 'Delete';
      remove.onclick = () => { records = []; render(); };
      row.append(edit, remove);
      list.append(row);
    }
  }
  panel.querySelector('[data-testid="crud-add"]').onclick = () => { field.value = ''; };
  panel.querySelector('[data-testid="crud-save"]').onclick = () => {
    records = [field.value]; field.value = ''; render();
  };
}
setup('Student', 'name'); setup('Course', 'title');
</script></body></html>"""


def test_browser_probe_waits_for_async_edit_form_before_filling():
    if not shutil.which("node") or not PLAYWRIGHT.exists():
        pytest.skip("Node/Playwright is not installed")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        spec = {
            "url": f"http://127.0.0.1:{server.server_port}/",
            "contract": {
                "crud_required": True,
                "backend_stack": "springboot",
                "api_contract": [{"methods": ["GET", "POST", "PUT", "DELETE"], "payload": {"title": "Probe"}}],
            },
        }
        result = subprocess.run(
            ["node", str(PROBE)], input=json.dumps(spec), text=True,
            capture_output=True, timeout=45, check=False,
        )
        evidence = json.loads(result.stdout)
        assert result.returncode == 0, evidence.get("message")
        assert evidence["uiCrud"] is True
        assert evidence["phase"] == "delete-row"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("course_broken,duplicate_marker,tabbed", [
    (False, False, False), (False, True, False), (False, False, True),
    (True, False, False),
])
def test_browser_probe_validates_both_entity_panels(course_broken, duplicate_marker, tabbed):
    if not shutil.which("node") or not PLAYWRIGHT.exists():
        pytest.skip("Node/Playwright is not installed")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            page = MULTI_PAGE
            if duplicate_marker:
                page = page.replace('<section data-testid="crud-panel-Student">',
                                    '<span data-testid="crud-panel-Student"></span><section data-testid="crud-panel-Student">')
                page = page.replace('<section data-testid="crud-panel-Course">',
                                    '<span data-testid="crud-panel-Course"></span><section data-testid="crud-panel-Course">')
                page = page.replace("<h2>Students</h2>",
                                    '<h2>Students</h2><span data-testid="crud-panel-Student"></span>')
                page = page.replace("<h2>Courses</h2>",
                                    '<h2>Courses</h2><span data-testid="crud-panel-Course"></span>')
            if tabbed:
                page = page.replace(
                    '<section data-testid="crud-panel-Student">',
                    '<nav><button id="show-students" aria-controls="management-view">Students</button>'
                    '<button id="show-courses" aria-controls="management-view">Courses</button></nav>'
                    '<section data-testid="crud-panel-Student">',
                )
                page = page.replace(
                    "setup('Student', 'name'); setup('Course', 'title');",
                    "setup('Student', 'name'); setup('Course', 'title');"
                    "const studentPanel = document.querySelector('section[data-testid=crud-panel-Student]');"
                    "const coursePanel = document.querySelector('section[data-testid=crud-panel-Course]');"
                    "coursePanel.remove();"
                    "document.getElementById('show-students').onclick = () => {"
                    "if (!studentPanel.isConnected) coursePanel.replaceWith(studentPanel); };"
                    "document.getElementById('show-courses').onclick = () => {"
                    "if (!coursePanel.isConnected) studentPanel.replaceWith(coursePanel); };",
                )
            if course_broken:
                page = page.replace("records = [field.value]; field.value = ''; render();",
                                    "if (panelId !== 'Course') records = [field.value]; field.value = ''; render();")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        spec = {
            "url": f"http://127.0.0.1:{server.server_port}/",
            "contract": {
                "crud_required": True, "backend_stack": "springboot",
                "api_contract": [
                    {"entity_id": "Student", "methods": ["GET", "POST", "PUT", "DELETE"], "payload": {"name": "A"}},
                    {"entity_id": "Course", "methods": ["GET", "POST", "PUT", "DELETE"], "payload": {"title": "B"}},
                ],
            },
        }
        result = subprocess.run(
            ["node", str(PROBE)], input=json.dumps(spec), text=True,
            capture_output=True, timeout=60, check=False,
        )
        evidence = json.loads(result.stdout)
        if course_broken:
            assert result.returncode != 0
            assert evidence["status"] == "failed"
            assert evidence["phase"] == "Course:create-visible-row"
            assert evidence["uiCrud"] is False
            return
        assert result.returncode == 0, evidence.get("message")
        assert evidence["uiCrud"] is True
        assert evidence["phase"] == "Course:delete-row"
        labels = {state["label"] for state in evidence["crudStates"]}
        assert {"Student-after-create", "Course-after-create", "Student-after-update", "Course-after-update"}.issubset(labels)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_browser_probe_prepares_related_product_but_tests_order_through_ui():
    if not shutil.which("node") or not PLAYWRIGHT.exists():
        pytest.skip("Node/Playwright is not installed")

    page = r'''<!doctype html><html><body><h1>Product and Order management</h1>
<section data-testid="crud-panel-Product"><button data-testid="crud-add">Add</button>
<input data-testid="crud-field-name"><input data-testid="crud-field-stock"><button data-testid="crud-save">Save</button><div class="list"></div></section>
<section data-testid="crud-panel-Order"><button data-testid="crud-add">Add</button>
<input data-testid="crud-field-productId"><input data-testid="crud-field-status"><button data-testid="crud-save">Save</button><div class="list"></div></section>
<script>
for (const [entity, fields, marker] of [['Product',['name','stock'],'name'],['Order',['productId','status'],'status']]) {
 const panel=document.querySelector(`[data-testid="crud-panel-${entity}"]`);
 const input=key=>panel.querySelector(`[data-testid="crud-field-${key}"]`);
 let record=null;
 function render(){ const list=panel.querySelector('.list'); list.replaceChildren(); if(!record)return;
  const row=document.createElement('div'); row.dataset.testid='crud-row'; row.append(document.createTextNode(record[marker]));
  const edit=document.createElement('button'); edit.dataset.testid='crud-edit'; edit.textContent='Edit';
  edit.onclick=()=>setTimeout(()=>fields.forEach(key=>input(key).value=record[key]),100);
  const remove=document.createElement('button'); remove.dataset.testid='crud-delete'; remove.textContent='Delete';
  remove.onclick=()=>{record=null;render()}; row.append(edit,remove);list.append(row);
 }
 panel.querySelector('[data-testid="crud-add"]').onclick=()=>fields.forEach(key=>input(key).value='');
 panel.querySelector('[data-testid="crud-save"]').onclick=async()=>{
  const next=Object.fromEntries(fields.map(key=>[key,input(key).value]));
  if(entity==='Order' && !(await fetch(`/api/products/${next.productId}`)).ok)return;
  record=next; fields.forEach(key=>input(key).value='');render();
 };
}
</script></body></html>'''
    fixtures = {"created": 0, "deleted": 0, "exists": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/products/42":
                body = b'{"id":42}' if fixtures["exists"] else b'{}'
                status = 200 if fixtures["exists"] else 404
                kind = "application/json"
            else:
                body = page.encode("utf-8")
                status = 200
                kind = "text/html; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/api/products"
            fixtures["created"] += 1
            fixtures["exists"] = True
            body = b'{"id":42}'
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            assert self.path == "/api/products/42"
            fixtures["deleted"] += 1
            fixtures["exists"] = False
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        spec = {
            "url": f"http://127.0.0.1:{server.server_port}/",
            "contract": {"crud_required": True, "backend_stack": "springboot", "api_contract": [
                {"entity_id": "Product", "path": "/api/products", "methods": ["GET", "POST", "PUT", "DELETE"],
                 "payload": {"name": "Product", "stock": 4}},
                {"entity_id": "Order", "path": "/api/orders", "methods": ["GET", "POST", "PUT", "DELETE"],
                 "payload": {"productId": 1, "status": "new"}},
            ]},
        }
        result = subprocess.run(["node", str(PROBE)], input=json.dumps(spec), text=True,
                                capture_output=True, timeout=60, check=False)
        evidence = json.loads(result.stdout)
        assert result.returncode == 0, evidence.get("message")
        assert evidence["uiCrud"] is True
        assert fixtures == {"created": 1, "deleted": 1, "exists": False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
