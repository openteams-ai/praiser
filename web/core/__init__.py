"""Framework-agnostic web core for praiser.

Nothing here imports a UI framework — it's the reusable layer (shared cache +
the ``praise()`` service) that any frontend (Streamlit today, FastAPI/Gradio
later) builds on.
"""
