# GeoLog

Laboratório de Persistência Poliglota com Streamlit, SQLite e MongoDB.

## Arquitetura

- **SQLite (`logitech.db`)**: motoristas e veículos.
- **MongoDB (`geolog_db.telemetria`)**: telemetria, sensores e coordenadas GeoJSON.
- **Streamlit**: interface, mapa, indicadores e gráficos.
- **Join poliglota**: feito em memória usando `veiculo_id`.

## Execução

### Windows (PowerShell)

Com Docker Desktop aberto:

```powershell
docker compose up -d
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

### Debian/Linux

```bash
docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

A aplicação abre em `http://localhost:8501`.

## Demonstração do SQLite

Abra `logitech.db` no VS Code usando uma extensão como **SQLite Viewer**.
Mostre as tabelas `motoristas` e `veiculos` e o relacionamento entre elas.

## Demonstração do MongoDB

Com o container em execução, abra o console:

```bash
docker exec -it geolog-mongodb mongosh
```

No Windows, execute o mesmo comando no PowerShell. No Debian, use `sudo` se
necessário. Em seguida:

```javascript
use geolog_db
```

### Documentos de telemetria

```javascript
db.telemetria.find().pretty()
```

### Índice geoespacial

```javascript
db.telemetria.getIndexes()
```

O resultado deve conter `location_2dsphere`, criado pelo código na inicialização.

### Busca por proximidade

```javascript
db.telemetria.find({
  location: {
    $near: {
      $geometry: {
        type: "Point",
        coordinates: [-34.873, -7.115]
      },
      $maxDistance: 10000
    }
  }
}).pretty()
```

Busca veículos em até 10 km do ponto informado. GeoJSON usa
`[longitude, latitude]` e a distância é em metros.

### Última telemetria por veículo

```javascript
db.telemetria.aggregate([
  { $sort: { veiculo_id: 1, timestamp: -1 } },
  { $group: {
      _id: "$veiculo_id",
      ultima: { $first: "$$ROOT" }
  }},
  { $replaceRoot: { newRoot: "$ultima" } }
])
```

### Alertas de velocidade

```javascript
db.telemetria.find(
  { velocidade: { $gt: 80 } },
  { _id: 0, veiculo_id: 1, velocidade: 1, timestamp: 1 }
).sort({ velocidade: -1 })
```

## Join poliglota

O SQLite fornece placa e motorista; o MongoDB fornece temperatura, velocidade
e localização. A aplicação consulta os dois bancos e une os resultados pelo
`veiculo_id` na função `join_poliglota` do `app.py`.

O resultado aparece na aba **Visão Unificada**:

```text
Motorista | Placa | Temperatura | Velocidade | Latitude | Longitude
```
