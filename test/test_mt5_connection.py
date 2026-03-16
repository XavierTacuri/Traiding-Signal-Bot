import MetaTrader5 as met5

if not met5.initialize():
    print("No se pudo conectar")
    print("Error", met5.last_error())
    quit()

print("Conectado")

terminal_info = met5.terminal_info()
print("Terminal:", terminal_info)

met5.shutdown()