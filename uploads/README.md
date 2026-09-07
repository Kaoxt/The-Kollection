# Artwork ZIP uploads

Upload category ZIP files into this folder.

Example:

- `uploads/discover.zip`
- `uploads/genres.zip`
- `uploads/studios.zip`

The GitHub Action in `.github/workflows/import-category-zips.yml` will:

1. Extract each ZIP into `images/`.
2. Preserve the category/folder structure inside the ZIP.
3. Remove common ZIP metadata such as `__MACOSX` and `.DS_Store`.
4. Delete the uploaded ZIP after a successful import.
5. Commit the extracted artwork back to `main`.

A ZIP with:

```text
Discover/
  Trending/
    backdrop.webp
    cover.webp
    logo.webp
```

becomes:

```text
images/
  Discover/
    Trending/
      backdrop.webp
      cover.webp
      logo.webp
```
