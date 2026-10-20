# Viz

- Run test servers only as `tools/viz/ocudu-viz --name agent --detach --no-browser ...`, never unnamed: a named session
  never touches the user's sessions. Stop it with `tools/viz/ocudu-viz --stop agent` before handing back. This is the
  Docker wrapper; the `ocudu-viz` of the Python package has none of these options.
