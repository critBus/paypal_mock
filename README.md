### Instalar dependencias

Con el `pyproject.toml` ya creado:

`uv sync`

Esto instalará todo lo que esté en tu `pyproject.toml` y fijará versiones en `uv.lock`.

---

### Ejecutar FastAPI con uv

Por ejemplo:

`uv run uvicorn main:app --reload`

`uv run uvicorn main:app --reload --port 7000`

---

# Docs

`http://127.0.0.1:7000/docs`
