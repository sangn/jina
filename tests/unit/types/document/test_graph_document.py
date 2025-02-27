import pytest

from jina.types.document.graph import GraphDocument
from jina.types.document import Document


@pytest.fixture()
def graph():
    graph = GraphDocument()

    doc0 = Document(text='Document0')
    doc1 = Document(text='Document1')
    doc2 = Document(text='Document2')
    doc3 = Document(text='Document3')

    graph.add_edge(doc0, doc1, features={'text': 'I connect Doc0 and Doc1'})
    graph.add_edge(doc0, doc2, features={'text': 'I connect Doc0 and Doc2'})
    graph.add_edge(doc2, doc1, features={'text': 'I connect Doc2 and Doc1'})
    graph.add_edge(doc1, doc3, features={'text': 'I connect Doc1 and Doc3'})
    graph.add_edge(doc2, doc3, features={'text': 'I connect Doc2 and Doc3'})
    return graph


def validate_graph(graph):
    assert graph.num_nodes == 4
    assert graph.num_edges == 5

    doc0 = graph.chunks[0]
    assert doc0.text == 'Document0'
    doc1 = graph.chunks[1]
    assert doc1.text == 'Document1'
    doc2 = graph.chunks[2]
    assert doc2.text == 'Document2'
    doc3 = graph.chunks[3]
    assert doc3.text == 'Document3'

    edge_features = graph.edge_features
    for i, (d1, d2) in enumerate(graph):
        if i == 0:
            assert (
                edge_features[f'{d1.id}-{d2.id}']['text'] == 'I connect Doc0 and Doc1'
            )
            assert d1.text == 'Document0'
            assert d2.text == 'Document1'
        if i == 1:
            assert (
                edge_features[f'{d1.id}-{d2.id}']['text'] == 'I connect Doc0 and Doc2'
            )
            assert d1.text == 'Document0'
            assert d2.text == 'Document2'
        if i == 2:
            assert (
                edge_features[f'{d1.id}-{d2.id}']['text'] == 'I connect Doc2 and Doc1'
            )
            assert d1.text == 'Document2'
            assert d2.text == 'Document1'
        if i == 3:
            assert (
                edge_features[f'{d1.id}-{d2.id}']['text'] == 'I connect Doc1 and Doc3'
            )
            assert d1.text == 'Document1'
            assert d2.text == 'Document3'
        if i == 4:
            assert (
                edge_features[f'{d1.id}-{d2.id}']['text'] == 'I connect Doc2 and Doc3'
            )
            assert d1.text == 'Document2'
            assert d2.text == 'Document3'

    assert graph.get_out_degree(doc0) == 2
    outgoing_0 = graph.get_outgoing_nodes(doc0)
    assert len(outgoing_0) == 2
    assert outgoing_0[0].text == 'Document1'
    assert outgoing_0[1].text == 'Document2'

    assert graph.get_in_degree(doc0) == 0
    incoming_0 = graph.get_incoming_nodes(doc0)
    assert len(incoming_0) == 0

    assert graph.get_out_degree(doc1) == 1
    outgoing_1 = graph.get_outgoing_nodes(doc1)
    assert len(outgoing_1) == 1
    assert outgoing_1[0].text == 'Document3'

    assert graph.get_in_degree(doc1) == 2
    incoming_1 = graph.get_incoming_nodes(doc1)
    assert len(incoming_1) == 2
    assert incoming_1[0].text == 'Document0'
    assert incoming_1[1].text == 'Document2'

    assert graph.get_out_degree(doc2) == 2
    outgoing_2 = graph.get_outgoing_nodes(doc2)
    assert len(outgoing_2) == 2
    assert outgoing_2[0].text == 'Document1'
    assert outgoing_2[1].text == 'Document3'

    assert graph.get_in_degree(doc2) == 1
    incoming_2 = graph.get_incoming_nodes(doc2)
    assert len(incoming_2) == 1
    assert incoming_2[0].text == 'Document0'

    assert graph.get_out_degree(doc3) == 0
    outgoing_3 = graph.get_outgoing_nodes(doc3)
    assert len(outgoing_3) == 0

    assert graph.get_in_degree(doc3) == 2
    incoming_3 = graph.get_incoming_nodes(doc3)
    assert len(incoming_3) == 2
    assert incoming_3[0].text == 'Document1'
    assert incoming_3[1].text == 'Document2'

    assert graph.get_incoming_nodes(Document()) is None
    assert graph.get_outgoing_nodes(Document()) is None


def test_graph_document_add_edges(graph):
    validate_graph(graph)


def test_graph_document_from_graph(graph):
    graph2 = GraphDocument(graph)
    validate_graph(graph2)


def test_graph_document_from_proto(graph):
    graph2 = GraphDocument(graph._pb_body)
    validate_graph(graph2)


def test_remove_nodes(graph):
    import copy

    nodes = copy.deepcopy(graph.nodes)

    for i, node in enumerate(nodes):
        num_nodes = graph.num_nodes
        num_edges = graph.num_edges
        num_edges_to_remove = graph.get_in_degree(node) + graph.get_out_degree(node)
        graph.remove_node(node)
        assert graph.num_nodes == num_nodes - 1
        assert graph.num_edges == num_edges - num_edges_to_remove

    assert graph.num_nodes == 0
    assert graph.num_edges == 0


def test_remove_edges(graph):
    edges = list([pair for pair in graph])

    num_edge_features = len(graph.edge_features.keys())
    for doc1, doc2 in edges:
        num_edges = graph.num_edges
        graph.remove_edge(doc1, doc2)
        num_edge_features -= 1
        assert graph.num_edges == num_edges - 1

    assert graph.num_nodes == 4  # nodes are not removed
    assert graph.num_edges == 0


def test_to_dgl_graph(graph):

    dgl_graph = graph.to_dgl_graph()
    dgl_adj_coo = dgl_graph.adjacency_matrix(scipy_fmt='coo')

    assert dgl_graph.num_nodes() == graph.num_nodes
    assert dgl_graph.num_edges() == graph.num_edges
    assert (graph.adjacency.row == dgl_adj_coo.row).all()
    assert (graph.adjacency.col == dgl_adj_coo.col).all()


def test_from_dgl_graph(graph):

    dgl_graph = graph.to_dgl_graph()
    jina_graph = GraphDocument.load_from_dgl_graph(dgl_graph)
    assert graph.num_nodes == jina_graph.num_nodes
    assert graph.num_edges == jina_graph.num_edges
    assert (graph.adjacency.col == jina_graph.adjacency.col).all()
    assert (graph.adjacency.col == jina_graph.adjacency.col).all()
