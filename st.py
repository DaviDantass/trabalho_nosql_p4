# GeoLog - Laboratório de Persistência Poliglota
#
# SQLite guarda os dados cadastrais e relacionais (motoristas e veículos).
# MongoDB guarda a telemetria e as coordenadas GeoJSON. O Streamlit integra
# os dois bancos em memória e apresenta mapa, indicadores e gráficos.
#
# Este arquivo é intencionalmente único para atender ao formato de entrega
# do laboratório: um arquivo .py executável com `streamlit run app.py`.

# `random`: gera pequenas variações no simulador de movimento.
import random
# `sqlite3`: biblioteca padrão para abrir e consultar o SQLite.
import sqlite3
# `datetime` cria timestamps; `timezone.utc` mantém as datas em UTC.
from datetime import datetime, timezone
# `Path` monta caminhos que funcionam independentemente do sistema operacional.
from pathlib import Path

# Folium cria o mapa HTML interativo.
import folium
# Pandas organiza os dados em DataFrames e faz o join em memória.
import pandas as pd
# Plotly gera os gráficos interativos.
import plotly.express as px
# Streamlit transforma este script em uma aplicação web.
import streamlit as st
# Componentes do PyMongo usados em índices, ordenações e conexão.
from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient
# Agrupa marcadores próximos no mapa para melhorar a visualização.
from folium.plugins import MarkerCluster
# Renderiza o mapa Folium dentro da página Streamlit.
from streamlit_folium import st_folium

# `__file__` é o caminho deste arquivo. Assim, o SQLite é encontrado mesmo
# quando o comando `streamlit run` é executado a partir de outra pasta.
BASE_DIR = Path(__file__).resolve().parent
SQLITE_DB = str(BASE_DIR / "logitech.db")

# Configurações do MongoDB. O Docker publica a porta 27017 localmente.
MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = "geolog_db"
MONGO_COLLECTION = "telemetria"

# Seed relacional: cada tupla segue a ordem das colunas da tabela motoristas.
MOTORISTAS = [
    (1, "Carlos Andrade", "123456789", "Ativo"),
    (2, "Mariana Silva", "987654321", "Ativo"),
    (3, "Roberto Souza", "456789123", "Em Descanso"),
]

# Seed relacional: motorista_id cria a ligação com motoristas.id.
VEICULOS = [
    (101, "ABC-1A23", "Volvo FH 540", 1),
    (102, "XYZ-9876", "Scania R450", 2),
    (103, "KGB-4567", "Mercedes Actros", 3),
]

# Seed NoSQL. Em GeoJSON, coordinates sempre segue [longitude, latitude].
TELEMETRIA_SEED = [
    {
        "veiculo_id": 101,
        "location": {"type": "Point", "coordinates": [-34.873, -7.115]},
        "temperatura": 4.2,
        "velocidade": 65,
        "timestamp": datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc),
    },
    {
        "veiculo_id": 102,
        "location": {"type": "Point", "coordinates": [-34.832, -7.121]},
        "temperatura": -18.5,
        "velocidade": 85,
        "timestamp": datetime(2026, 9, 11, 10, 5, tzinfo=timezone.utc),
    },
    {
        "veiculo_id": 103,
        "location": {"type": "Point", "coordinates": [-34.950, -7.150]},
        "temperatura": 22.0,
        "velocidade": 0,
        "timestamp": datetime(2026, 9, 11, 9, 45, tzinfo=timezone.utc),
    },
]

# Pontos usados no selectbox. A tupla segue (latitude, longitude), que é a
# ordem esperada pelo Folium e pela interface. Antes da consulta MongoDB, a
# função buscar_por_raio converte para [longitude, latitude].
PONTOS_REFERENCIA = {
    "Centro de João Pessoa": (-7.115, -34.873),
    "Cabo Branco": (-7.121, -34.832),
    "Tibiri / BR-230": (-7.150, -34.950),
}


def conectar_sqlite():
    """Abre o banco relacional local."""
    # Abre o arquivo SQLite. A função chamadora fecha a conexão ao terminar.
    return sqlite3.connect(SQLITE_DB, check_same_thread=False)


@st.cache_resource
def conectar_mongo():
    """Conecta ao MongoDB e confirma que o servidor está disponível."""
    # Timeout curto evita travar a interface se o Docker estiver desligado.
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    # Ping força uma tentativa real de conexão.
    client.admin.command("ping")
    return client[MONGO_DB][MONGO_COLLECTION]


def inicializar_sqlite():
    """Cria as tabelas relacionais e insere o seed sem duplicar registros."""
    conn = conectar_sqlite()
    # Cursor envia os comandos SQL para o banco.
    cursor = conn.cursor()

    # Tabela principal dos motoristas.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS motoristas (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            cnh TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL
        )
    """)

    # Tabela de veículos. motorista_id cria o relacionamento entre tabelas.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS veiculos (
            id INTEGER PRIMARY KEY,
            placa TEXT NOT NULL UNIQUE,
            modelo TEXT NOT NULL,
            motorista_id INTEGER NOT NULL,
            FOREIGN KEY (motorista_id) REFERENCES motoristas(id)
        )
    """)

    # `?` protege os valores; INSERT OR IGNORE torna o seed repetível.
    cursor.executemany(
        "INSERT OR IGNORE INTO motoristas (id, nome, cnh, status) VALUES (?, ?, ?, ?)",
        MOTORISTAS,
    )
    cursor.executemany(
        "INSERT OR IGNORE INTO veiculos (id, placa, modelo, motorista_id) VALUES (?, ?, ?, ?)",
        VEICULOS,
    )

    # Confirma os inserts e fecha o arquivo.
    conn.commit()
    conn.close()


def inicializar_mongo(collection):
    """Cria os índices e insere a telemetria inicial na primeira execução.

    O índice 2dsphere é indispensável para o MongoDB entender o campo
    `location` como GeoJSON e executar consultas geoespaciais por distância.
    """
    # Requisito obrigatório: índice geoespacial criado por código.
    # Índice obrigatório para consultas GeoJSON por proximidade.
    collection.create_index([("location", GEOSPHERE)], name="location_2dsphere")
    collection.create_index(
        [("veiculo_id", ASCENDING), ("timestamp", DESCENDING)],
        name="veiculo_timestamp_idx",
    )

    # Inserimos dados iniciais somente quando a coleção ainda está vazia.
    if collection.count_documents({}) == 0:
        collection.insert_many(TELEMETRIA_SEED)


def carregar_cadastros():
    """Consulta no SQLite a relação entre veículos e motoristas."""
    conn = conectar_sqlite()
    # Este JOIN é feito pelo próprio SQLite, antes do join poliglota.
    query = """
        SELECT
            v.id AS veiculo_id,
            v.placa,
            v.modelo,
            m.nome AS motorista,
            m.status AS status_motorista
        FROM veiculos v
        JOIN motoristas m ON m.id = v.motorista_id
        ORDER BY v.id
    """
    # Pandas transforma o resultado SQL em tabela para facilitar o merge.
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def ultimas_telemetrias(collection):
    """Usa agregação MongoDB para obter a telemetria mais recente por veículo."""
    pipeline = [
        {"$sort": {"veiculo_id": 1, "timestamp": -1}},
        {"$group": {"_id": "$veiculo_id", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"veiculo_id": 1}},
    ]
    return list(collection.aggregate(pipeline))


def join_poliglota(collection):
    """Une os dados do SQLite e do MongoDB em memória usando veiculo_id."""
    # Cada fonte é consultada separadamente.
    cadastros = carregar_cadastros()
    telemetrias = ultimas_telemetrias(collection)

    linhas = []
    # Cada documento Mongo é convertido em uma linha tabular.
    for item in telemetrias:
        longitude, latitude = item["location"]["coordinates"]
        linhas.append(
            {
                "veiculo_id": item["veiculo_id"],
                "ultima_temperatura": item["temperatura"],
                "velocidade": item["velocidade"],
                "latitude": latitude,
                "longitude": longitude,
                "timestamp": item["timestamp"],
            }
        )

    if not linhas:
        for coluna in [
            "ultima_temperatura", "velocidade", "latitude", "longitude", "timestamp"
        ]:
            cadastros[coluna] = pd.NA
        return cadastros

    # Este merge é o join poliglota: a chave comum está nos dois bancos.
    return cadastros.merge(pd.DataFrame(linhas), on="veiculo_id", how="left")


def buscar_por_raio(collection, latitude, longitude, raio_km):
    """Executa o `$near`; o MongoDB recebe a distância em metros."""
    # A interface usa km; o MongoDB usa metros. GeoJSON usa lon antes de lat.
    return list(
        collection.find(
            {
                "location": {
                    "$near": {
                        "$geometry": {
                            "type": "Point",
                            "coordinates": [longitude, latitude],
                        },
                        "$maxDistance": raio_km * 1000,
                    }
                }
            }
        )
    )


def historico_temperatura(collection, veiculo_id):
    """Prepara o histórico de um veículo para o gráfico Plotly."""
    # A ordenação cronológica é necessária para o gráfico de linha.
    dados = list(
        collection.find({"veiculo_id": int(veiculo_id)}).sort("timestamp", ASCENDING)
    )

    return pd.DataFrame(
        [
            {
                "timestamp": item["timestamp"],
                "temperatura": item["temperatura"],
                "velocidade": item["velocidade"],
            }
            for item in dados
        ]
    )


def simular_movimentacao(collection):
    """Insere novas posições aleatórias para implementar o desafio bônus."""
    # Cada clique gera um novo evento para cada veículo.
    for item in ultimas_telemetrias(collection):
        longitude, latitude = item["location"]["coordinates"]

        # insert_one preserva o histórico e adiciona um documento novo.
        collection.insert_one(
            {
                "veiculo_id": item["veiculo_id"],
                "location": {
                    "type": "Point",
                    "coordinates": [
                        longitude + random.uniform(-0.008, 0.008),
                        latitude + random.uniform(-0.008, 0.008),
                    ],
                },
                "temperatura": round(
                    float(item["temperatura"]) + random.uniform(-1.0, 1.0), 1
                ),
                "velocidade": max(
                    0, round(float(item["velocidade"]) + random.uniform(-12, 12))
                ),
                "timestamp": datetime.now(timezone.utc),
            }
        )


def montar_mapa(collection, latitude, longitude, raio_km):
    """Cria o mapa Folium, o círculo de busca e os marcadores dos veículos."""
    # Folium recebe coordenadas como [latitude, longitude].
    mapa = folium.Map(location=[latitude, longitude], zoom_start=11, control_scale=True)

    folium.Marker(
        [latitude, longitude],
        tooltip="Ponto de referência",
        popup=f"Raio: {raio_km:.1f} km",
        icon=folium.Icon(color="red", icon="info-sign"),
    ).add_to(mapa)

    folium.Circle(
        [latitude, longitude],
        radius=raio_km * 1000,
        tooltip=f"Raio de {raio_km:.1f} km",
        fill=True,
        fill_opacity=0.08,
    ).add_to(mapa)

    # Consulta o MongoDB e depois carrega os dados do SQLite para os popups.
    resultados = buscar_por_raio(collection, latitude, longitude, raio_km)
    cadastros = carregar_cadastros().set_index("veiculo_id").to_dict("index")

    cluster = MarkerCluster().add_to(mapa)

    # Cada documento encontrado vira um marcador no mapa.
    for item in resultados:
        veiculo_id = item["veiculo_id"]
        longitude_v, latitude_v = item["location"]["coordinates"]
        cadastro = cadastros.get(veiculo_id, {})
        velocidade = item["velocidade"]

        popup = (
            f"<b>{cadastro.get('placa', 'Sem placa')}</b><br>"
            f"Motorista: {cadastro.get('motorista', 'N/D')}<br>"
            f"Velocidade: {velocidade} km/h<br>"
            f"Temperatura: {item['temperatura']} °C"
        )

        folium.Marker(
            [latitude_v, longitude_v],
            tooltip=f"Veículo {veiculo_id}",
            popup=popup,
            icon=folium.Icon(
                color="orange" if velocidade > 80 else "blue",
                icon="truck",
                prefix="fa",
            ),
        ).add_to(cluster)

    return mapa, resultados


def main():
    # Streamlit reexecuta esta função quando o usuário altera um widget.
    """Executa o fluxo principal da aplicação Streamlit."""
    st.set_page_config(
        page_title="GeoLog",
        page_icon="🚚",
        layout="wide",
    )

    st.title("🚚 GeoLog — Telemetria Logística")
    st.caption("Persistência poliglota: SQLite + MongoDB GeoSpatial")

    # Banco relacional: tabelas motoristas e veiculos.
    inicializar_sqlite()

    try:
        collection = conectar_mongo()
        inicializar_mongo(collection)
    except Exception as exc:
        st.error(
            "Não foi possível conectar ao MongoDB. "
            "Execute `docker compose up -d` antes de iniciar o Streamlit."
        )
        st.code(str(exc))
        st.stop()

    with st.sidebar:
        # A barra lateral concentra os filtros e ações da aplicação.
        st.header("Busca geoespacial")
        ponto_nome = st.selectbox(
            "Localização de referência",
            list(PONTOS_REFERENCIA.keys()),
        )

        # Obtém as coordenadas do ponto selecionado.
        lat_padrao, lon_padrao = PONTOS_REFERENCIA[ponto_nome]
        raio_km = st.slider("Raio de busca (km)", 1.0, 30.0, 10.0, 1.0)

        st.divider()
        st.subheader("Bônus")

        if st.button("🚛 Simular Movimentação", use_container_width=True):
            simular_movimentacao(collection)
            st.success("Novos pontos de telemetria gravados no MongoDB.")
            st.rerun()

        if st.button("♻️ Resetar telemetria", use_container_width=True):
            collection.delete_many({})
            collection.insert_many(TELEMETRIA_SEED)
            st.rerun()

    # Join poliglota: cadastro do SQLite + última telemetria do MongoDB.
    df_unificado = join_poliglota(collection)

    # Os indicadores são calculados sobre o resultado do join poliglota.
    total_frotas_ativas = int(
        df_unificado["status_motorista"].eq("Ativo").sum()
    )
    media_temperatura = df_unificado["ultima_temperatura"].mean()
    alertas_velocidade = int((df_unificado["velocidade"] > 80).sum())

    # Divide os KPIs em três colunas.
    c1, c2, c3 = st.columns(3)
    c1.metric("Frotas ativas", total_frotas_ativas)
    c2.metric(
        "Temperatura média",
        "N/D" if pd.isna(media_temperatura) else f"{float(media_temperatura):.1f} °C",
    )
    c3.metric("Alertas > 80 km/h", alertas_velocidade)

    st.divider()

    # Cada aba evidencia uma parte dos requisitos do desafio.
    tab1, tab2, tab3 = st.tabs(
        ["🌍 Geoprocessamento", "🔗 Visão Unificada", "📊 Dashboard"]
    )

    with tab1:
        # Aba geoespacial: consulta $near e mapa Folium.
        st.subheader("Busca por raio com MongoDB `$near`")
        mapa, resultados = montar_mapa(
            collection, lat_padrao, lon_padrao, raio_km
        )
        st_folium(mapa, width=None, height=520, use_container_width=True)
        st.write(f"**{len(resultados)} registro(s)** dentro do raio selecionado.")

        if resultados:
            dados_raio = []
            for item in resultados:
                lon, lat = item["location"]["coordinates"]
                dados_raio.append(
                    {
                        "veiculo_id": item["veiculo_id"],
                        "temperatura": item["temperatura"],
                        "velocidade": item["velocidade"],
                        "latitude": lat,
                        "longitude": lon,
                    }
                )
            st.dataframe(pd.DataFrame(dados_raio), use_container_width=True)

    with tab2:
        # Aba de integração: mostra o join entre SQLite e MongoDB.
        st.subheader("Join poliglota em memória")

        exibicao = df_unificado[
            [
                "motorista",
                "placa",
                "ultima_temperatura",
                "velocidade",
                "latitude",
                "longitude",
            ]
        ].copy()

        exibicao.columns = [
            "Nome do Motorista",
            "Placa",
            "Última Temperatura (°C)",
            "Velocidade (km/h)",
            "Latitude",
            "Longitude",
        ]

        st.dataframe(exibicao, use_container_width=True, hide_index=True)

        st.info(
            "Motorista e placa vêm do SQLite; temperatura, velocidade e "
            "coordenadas vêm do MongoDB. O cruzamento ocorre na aplicação."
        )

    with tab3:
        # Aba analítica: histórico de temperatura e status dos motoristas.
        col_a, col_b = st.columns(2)

        with col_a:
            st.subheader("Temperatura por veículo")
            veiculo_id = st.selectbox(
                "Veículo",
                df_unificado["veiculo_id"].tolist(),
                format_func=lambda x: (
                    f"{x} — "
                    f"{df_unificado.loc[df_unificado['veiculo_id'] == x, 'placa'].iloc[0]}"
                ),
            )

            historico = historico_temperatura(collection, veiculo_id)

            if not historico.empty:
                fig_temp = px.line(
                    historico,
                    x="timestamp",
                    y="temperatura",
                    markers=True,
                    labels={
                        "timestamp": "Data/hora",
                        "temperatura": "Temperatura (°C)",
                    },
                )
                st.plotly_chart(fig_temp, use_container_width=True)

        with col_b:
            st.subheader("Status dos motoristas")

            status_df = (
                carregar_cadastros()["status_motorista"]
                .value_counts()
                .rename_axis("status")
                .reset_index(name="quantidade")
            )

            fig_status = px.pie(
                status_df,
                names="status",
                values="quantidade",
                hole=0.45,
            )
            st.plotly_chart(fig_status, use_container_width=True)

        st.subheader("Últimos registros de telemetria")
        st.dataframe(
            df_unificado[
                [
                    "veiculo_id",
                    "placa",
                    "motorista",
                    "ultima_temperatura",
                    "velocidade",
                    "timestamp",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()