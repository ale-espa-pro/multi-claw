class TokenUsageTracker:
    """Clase para rastrear y reportar el uso de tokens de OpenAI."""
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        """Resetea los contadores de tokens."""
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "context_window_tokens_by_agent": {}
        }
    
    def accumulate(self, response, agent_name=None):
        """Acumula el uso de tokens de una respuesta de OpenAI."""
        if hasattr(response, 'usage') and response.usage:
            usage = response.usage
            
            input_tokens = getattr(usage, 'input_tokens', 0) or 0
            output_tokens = getattr(usage, 'output_tokens', 0) or 0
            
            # Obtener cached tokens del detalle
            cached_tokens = 0
            if hasattr(usage, 'input_tokens_details') and usage.input_tokens_details:
                cached_tokens = getattr(usage.input_tokens_details, 'cached_tokens', 0) or 0
            
            self.usage["input_tokens"] += input_tokens
            self.usage["output_tokens"] += output_tokens
            self.usage["cached_tokens"] += cached_tokens
            self.usage["total_tokens"] += input_tokens + output_tokens
            self.usage["requests"] += 1

            if agent_name:
                self.usage["context_window_tokens_by_agent"][agent_name] = input_tokens
    
    def print_summary(self):
        """Imprime el resumen de uso de tokens."""
        print(f"\n\033[1;36m{'='*50}\033[0m")
        print(f"\033[1;36m  📊 TOKEN USAGE SUMMARY\033[0m")
        print(f"\033[1;36m{'='*50}\033[0m")
        print(f"  \033[1;33m📥 Input tokens:\033[0m  {self.usage['input_tokens']:,}")
        print(f"  \033[1;32m📤 Output tokens:\033[0m {self.usage['output_tokens']:,}")
        print(f"  \033[1;35m💾 Cached tokens:\033[0m {self.usage['cached_tokens']:,}")
        print(f"  \033[1;37m📊 Total tokens:\033[0m  {self.usage['total_tokens']:,}")
        print(f"  \033[1;34m🔄 API requests:\033[0m  {self.usage['requests']}")
        
        # Calcular porcentaje de cache si hay input tokens
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
        """Retorna una copia del uso de tokens actual."""
        return self.usage.copy()
