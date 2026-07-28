import os
import io
import json
import asyncio
import httpx
from PIL import Image
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from supabase import create_client, Client

app = FastAPI(title="API Nutrición & Supermercados Chile")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONEXIÓN A SUPABASE ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Referer": "https://www.lider.cl/",
}

HEADERS_CENCOSUD = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}


@app.get("/")
def inicio():
    return {"estado": "Servidor activo en Render 24/7 con Supabase 🔥"}


# --- RUTA DE IA: ANÁLISIS DE FOTOS CON GEMINI ---
@app.post("/api/analizar-plato")
async def analizar_plato(file: UploadFile = File(...)):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY no configurada")

    try:
        contenido_imagen = await file.read()
        imagen = Image.open(io.BytesIO(contenido_imagen))
        cliente = genai.Client(api_key=api_key)

        prompt = """
        Analiza detenidamente esta imagen de comida.
        1. Identifica el plato o los alimentos presentes.
        2. Estima la porción razonable en gramos para cada ingrediente visible.
        3. Calcula los macronutrientes totales (calorías, proteínas, carbohidratos, grasas).

        Responde ÚNICAMENTE con un objeto JSON válido sin bloques markdown:
        {
          "nombre_plato": "Nombre corto del plato",
          "porciones": [{"ingrediente": "Nombre", "gramos": 150}],
          "macros_totales": {"kcal": 450, "proteinas_g": 35, "carbohidratos_g": 40, "grasas_g": 15}
        }
        """

        respuesta = cliente.models.generate_content(
            model='gemini-2.5-flash',
            contents=[imagen, prompt]
        )

        texto_limpio = respuesta.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_limpio)
    except Exception as e:
        return {"error": "No se pudo analizar la imagen", "detalle": str(e)}


# --- BÚSQUEDA DE PRECIOS ---

async def buscar_en_lider(cliente: httpx.AsyncClient, producto: str):
    # NOTA: apps.lider.cl/bff/search da 404 -> Walmart Chile cambió esta ruta.
    # Pendiente reemplazar por la URL real (ver instrucciones abajo).
    try:
        url = f"https://apps.lider.cl/bff/search?query={producto}&page=1&facets="
        res = await cliente.get(url, headers=HEADERS, timeout=8.0)
        if res.status_code == 200:
            datos = res.json()
            productos = datos.get("products") or []
            if productos:
                p = productos[0]
                precio = p.get("price", {}).get("BasePriceSales")
                nombre = p.get("displayName")
                if precio is not None and nombre:
                    return {
                        "supermercado": "Lider",
                        "producto": nombre,
                        "precio": precio,
                    }
    except Exception as e:
        print(f"Error buscando en Lider ({producto}): {e}")
    return None


async def buscar_en_cencosud(cliente: httpx.AsyncClient, producto: str, marca: str):
    # VTEX descontinuó /api/catalog_system/pub/products/search/ (da 410 Gone).
    # Ahora se usa la Intelligent Search API v1, pública y sin autenticación.
    try:
        dominio = "jumbo.cl" if marca == "Jumbo" else "santaisabel.cl"
        url = f"https://www.{dominio}/api/io/_v/api/intelligent-search/product_search"
        params = {"query": producto, "count": 5}
        res = await cliente.get(url, headers=HEADERS_CENCOSUD, params=params, timeout=8.0)
        if res.status_code == 200:
            datos = res.json()
            productos = datos.get("products") or []
            if productos:
                p = productos[0]
                try:
                    precio = p["items"][0]["sellers"][0]["commertialOffer"]["Price"]
                except (KeyError, IndexError, TypeError):
                    precio = None
                nombre = p.get("productName")
                if precio is not None and nombre:
                    return {
                        "supermercado": marca,
                        "producto": nombre,
                        "precio": precio,
                    }
    except Exception as e:
        print(f"Error buscando en {marca} ({producto}): {e}")
    return None


@app.get("/api/precios")
async def obtener_precios(producto: str):
    busqueda_limpia = producto.lower().strip()

    if not busqueda_limpia:
        return {"busqueda": busqueda_limpia, "origen": "sin_busqueda", "resultados": []}

    if supabase:
        try:
            cache = supabase.table("precios_cache").select("*").eq("busqueda", busqueda_limpia).execute()
            if cache.data and len(cache.data) > 0:
                return {
                    "busqueda": busqueda_limpia,
                    "origen": "cache_supabase",
                    "resultados": [
                        {
                            "supermercado": item["supermercado"],
                            "producto": item["producto_nombre"],
                            "precio": item["precio"],
                        }
                        for item in cache.data
                    ],
                }
        except Exception as e:
            print(f"Error consultando caché Supabase: {e}")

    async with httpx.AsyncClient() as cliente:
        tareas = [
            buscar_en_lider(cliente, busqueda_limpia),
            buscar_en_cencosud(cliente, busqueda_limpia, "Jumbo"),
            buscar_en_cencosud(cliente, busqueda_limpia, "Santa Isabel"),
        ]
        resultados_crudos = await asyncio.gather(*tareas)

    resultados = [r for r in resultados_crudos if r is not None]

    if supabase and resultados:
        try:
            para_insertar = [
                {
                    "busqueda": busqueda_limpia,
                    "supermercado": r["supermercado"],
                    "producto_nombre": r["producto"],
                    "precio": r["precio"],
                }
                for r in resultados
            ]
            supabase.table("precios_cache").insert(para_insertar).execute()
        except Exception as e:
            print(f"Error guardando en caché Supabase: {e}")

    return {
        "busqueda": busqueda_limpia,
        "origen": "en_vivo" if resultados else "sin_resultados",
        "resultados": resultados,
    }


# --- ENDPOINT DE DIAGNÓSTICO (TEMPORAL) ---
# https://TU-BACKEND.onrender.com/api/debug-precios?producto=leche
@app.get("/api/debug-precios")
async def debug_precios(producto: str):
    busqueda_limpia = producto.lower().strip()
    resultado_diagnostico = {}

    async with httpx.AsyncClient() as cliente:
        try:
            url_lider = f"https://apps.lider.cl/bff/search?query={busqueda_limpia}&page=1&facets="
            res = await cliente.get(url_lider, headers=HEADERS, timeout=10.0)
            resultado_diagnostico["lider"] = {
                "status_code": res.status_code,
                "url": url_lider,
                "cuerpo_crudo": res.text[:500],
            }
        except Exception as e:
            resultado_diagnostico["lider"] = {"error": str(e)}

        try:
            url_jumbo = "https://www.jumbo.cl/api/io/_v/api/intelligent-search/product_search"
            res = await cliente.get(
                url_jumbo, headers=HEADERS_CENCOSUD,
                params={"query": busqueda_limpia, "count": 5}, timeout=10.0,
            )
            resultado_diagnostico["jumbo"] = {
                "status_code": res.status_code,
                "url": str(res.url),
                "cuerpo_crudo": res.text[:500],
            }
        except Exception as e:
            resultado_diagnostico["jumbo"] = {"error": str(e)}

        try:
            url_si = "https://www.santaisabel.cl/api/io/_v/api/intelligent-search/product_search"
            res = await cliente.get(
                url_si, headers=HEADERS_CENCOSUD,
                params={"query": busqueda_limpia, "count": 5}, timeout=10.0,
            )
            resultado_diagnostico["santa_isabel"] = {
                "status_code": res.status_code,
                "url": str(res.url),
                "cuerpo_crudo": res.text[:500],
            }
        except Exception as e:
            resultado_diagnostico["santa_isabel"] = {"error": str(e)}

    return resultado_diagnostico
