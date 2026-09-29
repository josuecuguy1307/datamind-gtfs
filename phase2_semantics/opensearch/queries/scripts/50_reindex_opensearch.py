index_def = {
    "settings": {
        "index": {
            "knn": True
        }
    },
    "mappings": {
        "properties": {
            "vector": {
                "type": "knn_vector",
                "dimension": 768
            },
            "name": {
                "type": "text"
            }
        }
    }
}
