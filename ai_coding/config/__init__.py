"""Configuration plumbing (Pydantic models + YAML loading).

Decoupled on purpose: ``AppConfig`` (AI/tools/memory/models) and ``MCPConfig``
are separate root models, corrected from the original single-document soup.
Dead keys that nothing reads (``allowedCommands``, ``allowedLanguages``) are
intentionally absent.
"""
