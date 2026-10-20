1. Add document types and obligation types as regulations grow
2. Why top_k_clause =5, why top_k in retrieval, keep = 6, n_gram = 4, 
3. Why dense = False? I used dense vectors
4. Why is retrieval BM25 only?
5. user login, logs
6. what apbu law / provision titles in search?
7. severity rubrics should extend beyond MRM
8. chatbot for question answering, not just one question
9. Add citations everywhere its possible
10. We can decide how much of the internal doc to process according to token logic, not top 5 caluses
11. For now we just note the references, not in obligations. Let's add references when extracting obligations, include reference text too and decide the obligation like that
12. max 12 passages is a wrong choice, lets give it as many as the token budget allows
13. We have a graph but what do we use it for in search in users questions? remake it into a chatbot, maybe something agentic
14. index.py why do we handwrite the values for rrf, when they should be coming from config
15. 900 chars cap, remove it, not needed
16. add chat sign owherever possible

