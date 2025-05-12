from config import actualizar_token_wise

async def obtener_token_wise():
    # Verifica si el token está en el caché
    token = await actualizar_token_wise()
    return token