from fastapi_cache.decorator import cache
import requests


# Función para actualizar y obtener el token desde la API de GECROS
@cache(expire=3000)  # Establece el tiempo de expiración solo aquí
async def actualizar_token_wise():
    """Obtiene un nuevo token desde la API y lo guarda."""
    url_token = 'https://api.wcx.cloud/core/v1/authenticate'
    query_params = {'user': 'apinobis'}
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': 'be9dd08a9cd8422a9af1372a445ec8e4',
    }
    try:
        response = requests.get(url_token, headers=headers, params=query_params)
        response.raise_for_status()  # Lanza un error si la solicitud falla
       
        # Imprimir la respuesta completa para verificar la estructura
        #print(f"Respuesta de la API: {response.text}")
       
        # Suponiendo que el token está en la clave 'token'
        response_json = response.json()
        token = response_json.get('token', None)
       
        if token:
            #print(f"Nuevo token obtenido y guardado: {token}")
            return token
        else:
            raise Exception("No se encontró el token en la respuesta.")
    except requests.exceptions.RequestException as e:
        print(f"Error al obtener el token: {e}")