class TokenUsageTracker:
    """Acumula uso de tokens normalizado por los providers."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "context_window_tokens_by_agent": {}
        }

    def accumulate(self, usage: dict, agent_name: str | None = None):
        """Acumula un dict de uso: {input_tokens, output_tokens, cached_tokens}."""
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cached_tokens = int(usage.get("cached_tokens", 0) or 0)

        self.usage["input_tokens"] += input_tokens
        self.usage["output_tokens"] += output_tokens
        self.usage["cached_tokens"] += cached_tokens
        self.usage["total_tokens"] += input_tokens + output_tokens
        self.usage["requests"] += 1

        if agent_name:
            self.usage["context_window_tokens_by_agent"][agent_name] = input_tokens

    def print_summary(self):
        print(f"\n\033[1;36m{'='*50}\033[0m")
        print(f"\033[1;36m  📊 TOKEN USAGE SUMMARY\033[0m")
        print(f"\033[1;36m{'='*50}\033[0m")
        print(f"  \033[1;33m📥 Input tokens:\033[0m  {self.usage['input_tokens']:,}")
        print(f"  \033[1;32m📤 Output tokens:\033[0m {self.usage['output_tokens']:,}")
        print(f"  \033[1;35m💾 Cached tokens:\033[0m {self.usage['cached_tokens']:,}")
        print(f"  \033[1;37m📊 Total tokens:\033[0m  {self.usage['total_tokens']:,}")
        print(f"  \033[1;34m🔄 API requests:\033[0m  {self.usage['requests']}")

        if self.usage['input_tokens'] > 0:
            cache_pct = (self.usage['cached_tokens'] / self.usage['input_tokens']) * 100
            print(f"  \033[1;35m📈 Cache hit rate:\033[0m {cache_pct:.1f}%")

        context_windows = self.usage.get("context_window_tokens_by_agent", {})
        if context_windows:
            print(f"  \033[1;36m🧠 Context window tokens by agent:\033[0m")
            for agent_name, tokens in sorted(context_windows.items()):
                print(f"    - {agent_name}: {tokens:,}")

        print(f"\033[1;36m{'='*50}\033[0m\n")

    def get_usage(self):
        return self.usage.copy()
