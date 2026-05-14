"""Diagnostic: time each import in the server startup chain."""
import time
import sys

def step(label, fn):
    print(f"-> {label} ...", flush=True)
    t = time.time()
    try:
        fn()
    except Exception as e:
        print(f"   FAIL after {time.time()-t:.1f}s: {type(e).__name__}: {e}", flush=True)
        return False
    print(f"   OK   {time.time()-t:.1f}s", flush=True)
    return True

step("import session",            lambda: __import__("session"))
step("import router",             lambda: __import__("router"))
step("import knowledge_pipeline.ingest_knowledge",
     lambda: __import__("knowledge_pipeline.ingest_knowledge", fromlist=["KnowledgeIngestor"]))
step("import server",             lambda: __import__("server", fromlist=["app"]))
print("DONE", flush=True)
