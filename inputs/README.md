Place client case JSON files in this folder before opening or refreshing the app.
Accepted formats: one client object, an array of clients, or {"Clients": [...]}.
Use the same field names as clients.json, including ClientRef and Portfolios.

The app discovers *.json files here. If none exist, it uses the bundled clients.json.
Choose a case and portfolio, then press Analyse and generate. The app collects
news through Apify itself; do not place news JSON in this folder.

reference.json in the project root is the shared reference export. New securities
not present there retain the normal missing-reference warnings and limitations.
Client files in this folder are ignored by Git. Refresh the browser after adding
or replacing a file. Changing file contents invalidates the previous displayed result.
