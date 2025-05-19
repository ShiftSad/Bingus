import { Body, Controller, Post } from '@nestjs/common';
import { SearchDto } from '../dto/search.dto';
import { SearchService, SearchResult } from '../search.service';

@Controller('search')
export class SearchController {
    constructor(
        private readonly searchService: SearchService
    ) { }

    @Post("query")
    async query(@Body() body: SearchDto): Promise<SearchResult[]> {
        const { query, limit, start } = body;
        return this.searchService.search(query, limit, start);
    }
}
