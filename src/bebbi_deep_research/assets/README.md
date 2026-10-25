# Assets

## `word/template.docx`

The Kanton Basel-Stadt Word template of Bebbi Bot's document tools, used by `services/docx_export.py` so the Deep
Research report looks like the Word files the chat writes (page, Arial, numbered headings, lists, table style, Baselstab
logo on the first page, "Seite n/m" footer).

- Source: FD-ITBS/kdkp-open-web-ui, `image/site-packages/kdkp_office/templates/template.docx`, commit `4811b91`
  (main), derived there from the OneOffixx letter template by `kdkp_office/prepare_template.py`.
- sha256: `8f666d500954644bfb2ae3cb18e045ab216852d9daf6904ca5738757c53288f5`

Copied unchanged. When kdkp-open-web-ui replaces its template, copy the new file here and run
`mise run test:unit` (`tests/unit/test_docx.py` checks the style names `docx_export` relies on: `Title`,
`Heading 1` to `Heading 4`, `ListWithBullets`, `ListWithNumbers`, `Table Grid`, `Block Text`, `Plain Text`, `Hyperlink`).
