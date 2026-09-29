# workspace/

**Runtime working folder** of the pipeline (review queues, generated catalogs, audits, outputs). Its
contents are git-ignored except this guide and `config/`.

- `config/supported_provinces.json` — active regions and their parameters. **Synthetic example**: replace it with your own region.

The code creates the subfolders it needs here when it uses them.
