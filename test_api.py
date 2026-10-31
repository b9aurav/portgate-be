import asyncio
import main

async def main_test():
    res = await main.audit(None, None, None, 5.0, True)
    print("API Audit mock returned status:", res.summary.status)
    print("Item 1 category:", res.items[0].category)
    print("Item 2 category:", res.items[1].category)
    print("Item 3 category:", res.items[2].category)
    print("Item 4 category:", res.items[3].category)
    print("All checks OK!")

if __name__ == "__main__":
    asyncio.run(main_test())

