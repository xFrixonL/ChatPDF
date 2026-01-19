import streamlit as st
import os
import hashlib
import chromadb
import pandas as pd
import google.generativeai as genai

from pypdf import PdfReader
from docx import Document
from bs4 import BeautifulSoup
import csv
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv

# ============================================================
# CONFIGURACIÓN GENERAL Y ENTORNO
# ============================================================
st.set_page_config(page_title="Chat Multiformato con Gemini", layout="wide")

# Carga las credenciales desde el archivo .env
load_dotenv()
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

# Inicialización del modelo de embeddings (convierte texto a vectores numéricos)
# 'all-MiniLM-L6-v2' es ligero y eficiente para ejecución local
EMBEDDING_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

# Inicialización de la base de datos vectorial ChromaDB
client = chromadb.Client()

# ============================================================
# FUNCIONES DE EXTRACCIÓN Y PROCESAMIENTO
# ============================================================

def hash_file(file) -> str:
    """
    Genera una huella digital (hash) única para el archivo.
    Sirve para detectar si el usuario subió un archivo nuevo o el mismo.
    """
    return hashlib.sha256(file.getvalue()).hexdigest()

def extract_text_from_file(file):
    """
    Función polimórfica que extrae el contenido textual de diversos formatos.
    Soporta: .pdf, .docx, .txt, .html, .csv y .xlsx.
    """
    file_extension = file.name.split('.')[-1].lower()
    text = ""

    # Caso PDF: Extrae texto página por página
    if file_extension == "pdf":
        reader = PdfReader(file)
        for i, page in enumerate(reader.pages):
            content = page.extract_text()
            if content: 
                text += f"\n[Fuente: {file.name} - Página {i+1}]\n{content}"
            
    # Caso Word: Itera sobre los párrafos del documento
    elif file_extension == "docx":
        doc = Document(file)
        text = "\n".join([para.text for para in doc.paragraphs])
        
    # Caso Texto Plano: Lectura directa con decodificación UTF-8
    elif file_extension == "txt":
        text = file.read().decode("utf-8")
        
    # Caso HTML: Limpia etiquetas y extrae solo el texto visible
    elif file_extension == "html":
        soup = BeautifulSoup(file.read(), "html.parser")
        text = soup.get_text(separator="\n")
        
    # Caso xlsx
    elif file_extension == "xlsx":
        df = pd.read_excel(file)
        text = df.to_string()

    elif file_extension == "csv":
        # 1. Leemos una muestra del archivo para detectar el separador
        sample = file.read(2048).decode("utf-8")
        file.seek(0) # "Rebobinamos" el archivo para que pandas lo lea desde el inicio
        
        try:
            # 2. Usamos el Sniffer de la librería csv para identificar el delimitador
            dialect = csv.Sniffer().sniff(sample)
            separador_detectado = dialect.delimiter
        except Exception:
            # 3. Si el Sniffer falla, usamos el punto y coma como respaldo
            separador_detectado = ';'

        # 4. Leemos con Pandas usando el separador automático
        try:
            df = pd.read_csv(file, sep=separador_detectado, encoding='utf-8')
        except:
            file.seek(0)
            df = pd.read_csv(file, sep=separador_detectado, encoding='latin-1')
            
        text = df.to_string()

    return text

def chunk_text(text):
    """
    Divide textos largos en fragmentos más pequeños (chunks).
    Esto es necesario porque los modelos de embedding y la IA tienen límites de tokens.
    """
    chunk_size = 800   # Caracteres por fragmento
    overlap = 160      # Caracteres que se repiten para no perder contexto entre cortes
    chunks = []
    start = 0
    chunk_id = 0

    while start < len(text):
        chunk_content = text[start:start + chunk_size]
        chunks.append({
            "id": f"chunk_{chunk_id}",
            "content": chunk_content,
            "start_index": start,
            "size": len(chunk_content)
        })
        chunk_id += 1
        start += chunk_size - overlap
    return chunks

# ============================================================
# FUNCIONES DE BASE DE DATOS Y RAG (VECTOR DB)
# ============================================================

def create_chroma_collection(chunks):
    """
    Toma los fragmentos de texto, genera sus vectores (embeddings) 
    y los guarda en la base de datos ChromaDB.
    """
    # Borramos la colección previa para evitar mezclar documentos distintos
    try:
        client.delete_collection("doc_rag")
    except:
        pass

    collection = client.create_collection(name="doc_rag")
    
    # Preparamos los datos para insertar
    texts = [c["content"] for c in chunks]
    ids = [c["id"] for c in chunks]
    metadatas = [{"chunk_index": i, "start_index": c["start_index"], "chunk_size": c["size"]} for i, c in enumerate(chunks)]
    
    # Generamos los vectores numéricos
    embeddings = EMBEDDING_MODEL.encode(texts)
    
    # Guardamos todo en la base de datos
    collection.add(
        documents=texts,
        embeddings=embeddings.tolist(),
        ids=ids,
        metadatas=metadatas
    )
    return collection

def retrieve_context(collection, query, k=4):
    """
    Busca en la base de datos los 'k' fragmentos que más se parecen 
    semánticamente a la pregunta del usuario.
    """
    query_embedding = EMBEDDING_MODEL.encode([query])
    return collection.query(query_embeddings=query_embedding.tolist(), n_results=k)

# ============================================================
# FUNCIÓN DE INTELIGENCIA ARTIFICIAL
# ============================================================

def ask_gemini(context, question):
    """
    Envía el contexto recuperado y la pregunta a Google Gemini.
    Usa un prompt de sistema para asegurar que no invente información.
    """
    model = genai.GenerativeModel("models/gemini-2.5-flash-lite")
    
    prompt = f"""
    Eres un asistente que responde SOLO con la información del contexto.
    Si la respuesta no está en el contexto, di: "No se encuentra en el documento".

    CONTEXTO:
    {context}

    PREGUNTA:
    {question}
    """
    
    response = model.generate_content(prompt)
    return response.text

# ============================================================
# INTERFAZ DE USUARIO (STREAMLIT)
# ============================================================

# Inicialización de estados de sesión para persistencia
if "collection" not in st.session_state: st.session_state.collection = None
if "file_processed" not in st.session_state: st.session_state.file_processed = False
if "file_hash" not in st.session_state: st.session_state.file_hash = None

st.title("🤖 Asistente Documental Inteligente")
st.markdown("Sube un archivo (PDF, Word, Excel, CSV, TXT o HTML) y chatea con su contenido.")

# Configuración del cargador de archivos
uploaded_file = st.file_uploader(
    "Selecciona un documento", 
    type=["pdf", "docx", "txt", "html", "csv", "xlsx"]
)

# Lógica de detección de cambios en el archivo
if uploaded_file:
    current_hash = hash_file(uploaded_file)
    if st.session_state.file_hash != current_hash:
        st.session_state.file_hash = current_hash
        st.session_state.file_processed = False
        st.session_state.collection = None

# Botón para disparar el procesamiento
if uploaded_file and not st.session_state.file_processed:
    if st.button("📥 Procesar e Indexar Contenido"):
        with st.spinner(f"Analizando {uploaded_file.name}..."):
            # 1. Extraer
            text = extract_text_from_file(uploaded_file)
            if text.strip():
                # 2. Fragmentar
                chunks = chunk_text(text)
                # 3. Vectorizar y Guardar
                st.session_state.collection = create_chroma_collection(chunks)
                st.session_state.file_processed = True
                st.success(f"¡Listo! Se han indexado {len(chunks)} fragmentos.")
            else:
                st.error("El archivo parece estar vacío o no tiene texto extraíble.")

# ------------------------------
# SECCIÓN DE PREGUNTAS (Con tu visualización original)
# ------------------------------
if st.session_state.file_processed and st.session_state.collection:
    st.divider()
    st.subheader("❓ Pregunta al documento")

    question = st.text_input("Escribe tu pregunta")

    if st.button("🤖 Preguntar") and question:
        with st.spinner("Buscando respuesta..."):
            results = retrieve_context(st.session_state.collection, question)

            # Unimos los documentos para Gemini
            context_text = "\n\n".join(results["documents"][0])

            answer = ask_gemini(context_text, question)

        st.subheader("🤖 Respuesta")
        st.write(answer)

        # ------------------------------
        # DETALLE DEL CONTEXTO USADO (Restaurado exactamente como lo tenías)
        # ------------------------------
        with st.expander("📚 Contexto usado (detallado)"):
            for i, (doc, meta) in enumerate(
                zip(results["documents"][0], results["metadatas"][0])
            ):
                st.markdown(f"""
**Chunk #{meta['chunk_index']}**
- 📍 Inicio en texto: `{meta['start_index']}`
- 📏 Tamaño: `{meta['chunk_size']}` caracteres

```text
{doc}
""")