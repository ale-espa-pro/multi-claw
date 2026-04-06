total_tools = [
    {
        "type": "function",
        "name": "search_files",
        "description": "Search files recursively using fuzzy matching against filenames.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "multiple mixed search queries for maximal generalization"
                },
                "root": {
                    "type": "string",
                    "description": "Root directory"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results +100 recommended"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "list_mcps",
        "description": "Muestra los MCPs disponibles y su configuración",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Que se desea buscar en almacenamiento de MCPs"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "create_simple_cron",
        "description": """Esta herramienta se usará cuando el usuario quiera ejecutar una acción o consulta en un día
        determinado o de forma reiterada""",
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "La acción contextualizada que se desea realizar (No se debe mencionar nada de que sea una tarea programada)"
                },
                "date": {
                    "type": "string",
                    "description": "Los datos o posibles intenciones/acciones que se quieran buscar en la memoria del agente"
                },
                "specificity": {
                    "type": "string",
                    "description": "Número entre 1 y 5 que describa si se tiene muy claro que se tiene que consultar 5 y a menos específico bajará hasta 1"
                },
                "type": {
                    "type": "string",
                    "description": "Si está claro se debe aclarar si la consulta es sobre una de las siguientes opciones: preference|action|info"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "create_agent_cron",
        "description": """Esta herramienta se usará cuando el usuario quiera ejecutar una acción o consulta en un día
        determinado o de forma reiterada""",
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "La acción contextualizada que se desea realizar (No se debe mencionar nada de que sea una tarea programada)"
                },
                "date": {
                    "type": "string",
                    "description": "Los datos o posibles intenciones/acciones que se quieran buscar en la memoria del agente"
                },
                "specificity": {
                    "type": "string",
                    "description": "Número entre 1 y 5 que describa si se tiene muy claro que se tiene que consultar 5 y a menos específico bajará hasta 1"
                },
                "type": {
                    "type": "string",
                    "description": "Si está claro se debe aclarar si la consulta es sobre una de las siguientes opciones: preference|action|info"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Crea o escribe contenido en un archivo de texto. USAR SIEMPRE para crear archivos en vez de run_python. Rutas permitidas: ~/Downloads, ~/Documents, ~/Desktop, /tmp.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta absoluta del archivo a crear/escribir"
                },
                "content": {
                    "type": "string",
                    "description": "Contenido de texto a escribir en el archivo"
                },
                "mode": {
                    "type": "string",
                    "description": "'w' para sobreescribir (default), 'a' para añadir al final",
                    "enum": ["w", "a"]
                }
            }
        },
        "required": ["path", "content"]
    },
    {
        "type": "function",
        "name": "run_command",
        "description": "Ejecuta un comando de terminal (bash/shell). Usar para: instalar paquetes (apt, pip), listar procesos, gestión de servicios, operaciones de sistema, git, docker, etc. NO usar para acciones destructivas (rm -rf /, sudo, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Comando de terminal a ejecutar"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout máximo en segundos (default 10, max 30)"
                },
                "workdir": {
                    "type": "string",
                    "description": "Directorio de trabajo (default: home del usuario)"
                }
            }
        },
        "required": ["command"]
    },
    {
        "type": "function",
        "name": "run_python",
        "description": "Ejecuta código Python en un sandbox RESTRINGIDO. SOLO para cálculos y lógica pura (math, strings, listas). NO tiene acceso a open(), import, ni I/O de archivos. Para escribir archivos usa write_file. Para comandos de sistema usa run_command.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Código Python a ejecutar (solo builtins básicos disponibles: abs, min, max, sum, len, range, sorted, etc.)"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout máximo en segundos (max 10)"
                }
            }
        },
        "required": ["code"]
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read and extract content from files like pdf, pptx, docx, csv, xlsx, txt, images.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative file path"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return for text files"
                }
            }
        },
        "required": ["path"]
    },
    {
        "type": "function",
        "name": "ask_user",
        "description": """Esta herramienta se usará cuando se necesite concretar detalles de la ejcución o permisos para acción crítica
        sin interrumpir el flujo de ejecución ni derivar en otros agentes""",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Pregunta que se le desea hacer al usuario"
                },
            }
        },
        "required": ["question"]
    },
    {
        "type": "function",
        "name": "save_preference",
        "description": """Esta herramienta se usará cuando se detecte una preferencia del usuario o algun comportamiento repetitivo o información 
        crítica del usuario que se quiera almacenar""",
        "parameters": {
            "type": "object",
            "properties": {
                "preference": {
                    "type": "string",
                    "description": "Información simple y descriptiva de lo que se desea almacenar. Maximo 40 palabras"
                },
            }
        },
        "required": ["preference"]
    },
    {
        "type": "function",
        "name": "web_fetch",
        "description": "Descarga el contenido HTML/JSON de una URL. Usar para consultar APIs, leer páginas web estáticas o verificar endpoints. Para páginas con JS dinámico o interacción usa playwright_navigate.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL completa a descargar (incluyendo https://)"
                },
                "method": {
                    "type": "string",
                    "description": "'GET' (default) o 'POST'",
                    "enum": ["GET", "POST"]
                },
                "headers": {
                    "type": "object",
                    "description": "Cabeceras HTTP opcionales (e.g. Authorization, Content-Type)"
                },
                "data": {
                    "type": "object",
                    "description": "Cuerpo de la petición para POST (se enviará como JSON)"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout en segundos (default 15, max 60)"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Máximo de caracteres a retornar del cuerpo de la respuesta"
                }
            }
        },
        "required": ["url"]
    },
    {
        "type": "function",
        "name": "playwright_navigate",
        "description": "Navega e interactúa con páginas web usando un navegador real (Chromium headless). Usar para páginas con JavaScript dinámico, formularios, scraping avanzado o capturas de pantalla. Para páginas estáticas o APIs usar web_fetch.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL completa a navegar (incluyendo https://)"
                },
                "action": {
                    "type": "string",
                    "description": "Acción a realizar: 'navigate' (cargar y devolver HTML), 'click' (hacer clic en un elemento), 'fill' (rellenar un campo), 'get_text' (obtener texto de un elemento), 'screenshot' (captura de pantalla en base64)",
                    "enum": ["navigate", "click", "fill", "get_text", "screenshot"]
                },
                "selector": {
                    "type": "string",
                    "description": "Selector CSS o XPath del elemento (requerido para click, fill, get_text)"
                },
                "value": {
                    "type": "string",
                    "description": "Texto a introducir en el campo (requerido para fill)"
                },
                "wait_for": {
                    "type": "string",
                    "description": "Selector CSS a esperar antes de retornar (útil para contenido dinámico)"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout en segundos (default 15, max 60)"
                },
                "headless": {
                    "type": "boolean",
                    "description": "Ejecutar sin interfaz gráfica (default true)"
                }
            }
        },
        "required": ["url"]
    },
    {
        "type": "function",
        "name": "memory_search",
        "description": """Esta herramienta tiene accceso a todo el historial de connversaciones y acciones de todos los agentes. 
        Ante la minima duda de un evento, pasado, acción o dato, se debe buscar. Especialmente util en el caso de busqueda de información para 
        workflows pasados, recopilar tareas cron, resultados de las mismas, diferencias, etc""",
        "parameters": {
            "type": "object",
            "properties": {
                "vector_search": {
                    "type": "string",
                    "description": "Texto de búsqueda optimizado para busqueda vectorial"
                },
                "K": {
                    "type": "string",
                    "description": "Limite de resultados por busqueda (default:5, max:25)"
                },
                "session_id": {
                    "type":"string",
                    "description": "En caso de saber la conversación/session o querer indagar mas concretamente en alguna sesion concreta se usará este campo para el fitrado"
                }
            }
        },
        "required": ["vectorSearch", "K"]
    },
    {
        "type": "function",
        "name": "store_actions",
        "description": """Esta herramineta se usará para almacenar un historial resumido 10-1000 palabras 
        con lo que se ha realizado a lo largo de la conversación""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta web deseada"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "WebSearchAgent",
        "description": "Busca información en la web según objetivos solicitados",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta web deseada"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "DeviceManagerAgent",
        "description": """Hace cualquier gestión a nivel del servidor/dispositivo: crear/leer archivos, buscar archivos,
        ejecutar comandos de terminal, cálculos python, etc.""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta deseada"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "MemoryAgent",
        "description": """Esta herramienta se usará siempre que se necesite saber la preferencia del usuario pasadas,
        Cambios que se hicieron,procesos o ejecuciones o cualquier dato que pueda ser necesario recuperar de una interacción pasada""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Los datos o posibles intenciones/acciones que se quieran buscar en la memoria del agente"
                },
                "specificity": {
                    "type": "string",
                    "description": "Número entre 1 y 5 que describa si se tiene muy claro que se tiene que consultar 5 y a menos específico bajará hasta 1"
                },
                "type": {
                    "type": "string",
                    "description": "Si está claro se debe aclarar si la consulta es sobre una de las siguientes opciones: preference|action|info"
                }
            }
        },
        "required": ["query"]
    },
    {
        "type": "function",
        "name": "ExecutorAgent",
        "description": """Ejecuta el plan detallado generado por el planner usando el siguiente formato JSON: 
        [{\"step_id\": \"<id>\", \"descripcion\": \"<Accion atomica>\", \"input\": \"<string>\", \"expected_output\": \"<string>\"}]""",
        "parameters": {
            "type": "object",
            "properties": {
                "execution_plan": {
                    "type": "array",
                    "description": "Plan de ejecución paso a paso",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "descripcion": {"type": "string"},
                            "input": {"type": "string"},
                            "expected_output": {"type": "string"}
                        },
                        "required": ["step_id", "descripcion", "input", "expected_output"]
                    }
                }
            }
        },
        "required": ["execution_plan"]
    }
]


def adapt_tools_for_langchain(tools):
    adapted = {}
    for t in tools:
        adapted[t["name"]] = {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"]
            }
        }
    return adapted


#dict_total_tools = adapt_tools_for_langchain(total_tools)
dict_total_tools = {tool["name"]: tool for tool in total_tools}