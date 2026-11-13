import { useState } from "react";

const ESTACIONES = [
  { id: 1, nombre: "Recibir" },
  { id: 2, nombre: "Limpiar" },
  { id: 3, nombre: "Editar" },
  { id: 4, nombre: "Mejorar / Marketing" },
  { id: 5, nombre: "Publicar" },
];

export default function App() {
  const [url, setUrl] = useState("");
  const [estados, setEstados] = useState(
    ESTACIONES.map(() => "Pendiente")
  );
  const [clips, setClips] = useState([]);

  function procesar() {
    // Despues: aqui va la llamada a Python
    // POST /procesar  { url }
    alert("Falta conectar con Python. Primero bajar.py.");
  }

  return (
    <main>
      <h1>Clip Agentes</h1>
      <input value={url} onChange={(e) => setUrl(e.target.value)}
        placeholder="Pega el enlace de TU video" />
      <button onClick={procesar}>Procesar</button>
      {ESTACIONES.map((s, i) => (
        <div key={s.id}>
          <strong>{s.id}. {s.nombre}</strong> - {estados[i]}
        </div>
      ))}
      <h2>Clips terminados</h2>
      <ul>{clips.map((c) => <li key={c}>{c}</li>)}</ul>
    </main>
  );
}
