"""Provider adapters that need more than a single cached GET (Phase 2 optional ATS providers).

Everything in here must keep the rest of the pipeline provider-agnostic: Stage 2 gets a
resolved board record, Stage 3 gets a list of normalized postings, and no downstream
stage knows the provider exists.
"""
