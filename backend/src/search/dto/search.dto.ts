import { IsString, IsNotEmpty, IsInt, Min, Max } from 'class-validator';

export class SearchDto {
    @IsString({ message: 'Query must be a string' })
    @IsNotEmpty({ message: 'Query cannot be empty' })
    query: string;

    @IsNotEmpty({ message: 'Limit cannot be empty' })
    @IsInt({ message: 'Limit must be a number' })
    @Min(1, { message: 'Limit must be at least 1' })
    @Max(100, { message: 'Limit cannot exceed 100' })
    limit: number;

    @IsInt({ message: 'Start must be a number' })
    @Min(0, { message: 'Start must be at least 0' })
    start: number;
}