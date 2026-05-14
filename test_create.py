import asyncio
from notebooklm import NotebookLMClient

async def test():
    try:
        async with await NotebookLMClient.from_storage() as client:
            print("Creating notebook...")
            nb = await client.notebooks.create(title='Endocrine Test')
            print(f"Created: {nb.id}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
