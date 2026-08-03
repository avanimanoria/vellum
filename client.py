import requests

BASE_URL = "http://127.0.0.1:8000"
EXTERNAL_USER_ID = "user_demo_001"
SESSION_ID = "9e69b2c3-5742-4031-848e-4844b995cb7f"

def get_memory_pack():
    url = f"{BASE_URL}/users/{EXTERNAL_USER_ID}/memory-pack"
    resp = requests.get(url)
    print("memory-pack status:", resp.status_code)
    print("memory-pack body:", resp.text)
    resp.raise_for_status()
    return resp.json()


def post_chat(message: str, salience: float = 0.9, token_count: int = 10):
    url = f"{BASE_URL}/chat"
    payload = {
        "external_user_id": EXTERNAL_USER_ID,
        "session_id": SESSION_ID,
        "message": message,
        "salience_score": salience,
        "token_count": token_count
    }
    resp = requests.post(url, json=payload)
    print("chat status:", resp.status_code)
    print("chat body:", resp.text)
    resp.raise_for_status()
    return resp.json()


def run_consolidation():
    url = f"{BASE_URL}/admin/consolidate/{EXTERNAL_USER_ID}"
    resp = requests.post(url)
    print("consolidation status:", resp.status_code)
    print("consolidation body:", resp.text)
    resp.raise_for_status()
    return resp.json()


def pretty_print_memory(memory_pack):
    print("\n=== MEMORY PACK ===")
    print(f"user_id: {memory_pack['user_id']}")
    for item in memory_pack["items"]:
        print(
            f"- {item['item_type']:12} | "
            f"{str(item['label_1']):20} | "
            f"{str(item['label_2'])} "
            f"(score_1={item['score_1']}, score_2={item['score_2']}, ts={item['ts']})"
        )


def main():
    print("Vellum Memory Client")
    print("Type 'exit' to quit.")
    print("Type 'consolidate' to run consolidation.\n")

    while True:
        text = input("You: ").strip()

        if text.lower() in {"exit", "quit"}:
            break

        if text.lower() == "consolidate":
            print("\n[POST] running consolidation...")
            result = run_consolidation()
            print(result)
            print("\n[GET] fetching memory pack...")
            memory = get_memory_pack()
            pretty_print_memory(memory)
            print("\n" + "-" * 80 + "\n")
            continue

        print("\n[POST] sending chat...")
        result = post_chat(text, salience=0.9, token_count=len(text.split()))
        print(f"assistant reply: {result['reply']}")
        print(f"used_memory: {result['used_memory']}")

        print("\n[GET] fetching updated memory pack...")
        memory = get_memory_pack()
        pretty_print_memory(memory)
        print("\n" + "-" * 80 + "\n")


if __name__ == "__main__":
    main()