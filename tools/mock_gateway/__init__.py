"""Development-only mock of Module 3 (Execution Gateway).

Completely independent of the Workbench: it imports no Workbench code and the
Workbench imports none of it. The only channel between them is HTTP through
MOD3_BASE_URL, and the only thing both read is the frozen contract/ schemas.
Replacing this with Sadia's real Gateway is an integration exercise, not a
Workbench change.
"""
